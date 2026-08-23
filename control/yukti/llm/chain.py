"""The layered client: try providers in order, fall through, then give up cleanly.

This is what makes the LLM layer safe to depend on lightly and safe to run with
nothing configured at all. It resolves an ordered list of providers and asks
each in turn until one answers. When none does, it raises `AllProvidersFailed`
carrying every failure — and the callers in `agent/specialists.py` turn that
into the conservative default they would have used anyway.

Three properties do the real work:

**A provider with no key costs nothing.** It is skipped before any socket is
opened, which is what makes a nine-provider chain cheap rather than a nine-way
timeout.

**A provider that fails permanently is disabled for the process.** A blocked
host or a bad key does not improve on the next call, and without a circuit
breaker every decision would pay its connect timeout again. This matters here
specifically: the build environment's egress policy 403s most providers, so
without the breaker the chain would be slower than having no chain at all.

**The cache is checked before the chain and written after it.** A repeated
question costs nothing and reproduces exactly, which is what makes a live demo
survive a rate limit.

Note what the chain does NOT do: it never falls back to a *different question*,
never relaxes a schema to get an answer, and never returns a partial result. It
either produces something that validated against the requested schema, or it
reports that nothing did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from yukti.llm import cache as cache_mod
from yukti.llm.anthropic_native import AnthropicClient
from yukti.llm.errors import Disposition, Failure, classify
from yukti.llm.openai_compat import OpenAICompatClient
from yukti.llm.registry import ProviderSpec, resolve_order

log = logging.getLogger(__name__)


class AllProvidersFailed(RuntimeError):
    """Every configured provider declined. Carries the reasons, in order."""

    def __init__(self, failures: list[Failure]) -> None:
        self.failures = failures
        detail = "; ".join(str(f) for f in failures) or "no providers configured"
        super().__init__(f"no LLM provider could serve the request — {detail}")


@dataclass(frozen=True, slots=True)
class Completion:
    """A validated answer, and an honest record of where it came from."""

    parsed: BaseModel
    provider: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    from_cache: bool = False


@dataclass
class ProviderState:
    spec: ProviderSpec
    disabled: bool = False
    last_failure: Failure | None = None
    calls: int = 0
    successes: int = 0

    @property
    def status(self) -> str:
        if not self.spec.configured():
            return "no key"
        if self.disabled:
            return f"disabled: {self.last_failure.reason}" if self.last_failure else "disabled"
        if self.successes:
            return "ok"
        return "configured"


class LayeredClient:
    def __init__(
        self, order: str | list[str] | None = None,
        cache: cache_mod.ResponseCache | None = None,
    ) -> None:
        self.states = [ProviderState(spec=s) for s in resolve_order(order)]
        self.cache = cache if cache is not None else cache_mod.ResponseCache()
        self._clients: dict[str, Any] = {}

    # -- introspection, for `yukti llm-status` -------------------------------

    def report(self) -> list[dict[str, Any]]:
        return [
            {
                "provider": s.spec.name,
                "configured": s.spec.configured(),
                "status": s.status,
                "base_url": s.spec.base_url or "(sdk default)",
                "key_env": ", ".join(s.spec.key_env) or "(none needed)",
                "free_tier": s.spec.free_tier,
                "calls": s.calls,
                "successes": s.successes,
                "last_failure": str(s.last_failure) if s.last_failure else "",
            }
            for s in self.states
        ]

    @property
    def any_configured(self) -> bool:
        return any(s.spec.configured() and not s.disabled for s in self.states)

    # -- the call ------------------------------------------------------------

    def complete(
        self, *, system: str, prompt: str, schema: type[BaseModel],
        tier: str = "planner", max_tokens: int = 4096, use_cache: bool = True,
        validate: Callable[[BaseModel], bool] | None = None,
    ) -> Completion:
        """Ask the chain for a structured answer.

        `validate` is a caller-supplied check that runs BEFORE the answer is
        cached. It exists because schema validity is not the same as usefulness:
        the RCA specialist rejects a narrative citing an evidence id it was never
        shown, and that answer is perfectly well-formed.

        Without this hook such an answer was still cached, then re-served and
        re-rejected on every subsequent call — so one bad sample permanently
        pinned that question to the fallback and the model was never asked
        again. The system stayed safe and got quietly worse, which is the
        failure shape this project keeps finding.

        A rejected answer is neither cached nor returned: the chain moves to the
        next provider, since another model may do better.
        """
        key = cache_mod.key_for(system, prompt, schema.__name__, tier)

        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                try:
                    return Completion(
                        parsed=schema.model_validate(hit.payload),
                        provider=hit.provider, model=hit.model, from_cache=True,
                    )
                except Exception:  # noqa: BLE001
                    # The schema changed since this was written. A stale entry
                    # is a miss, never a wrong answer served confidently.
                    log.info("cached response no longer matches %s", schema.__name__)

        failures: list[Failure] = []
        for state in self.states:
            if not state.spec.configured():
                failures.append(Failure(state.spec.name, Disposition.FALL_THROUGH,
                                        "not configured"))
                continue
            if state.disabled:
                failures.append(state.last_failure or Failure(
                    state.spec.name, Disposition.DISABLE, "disabled earlier"))
                continue

            result = self._try(state, system, prompt, schema, tier, max_tokens, failures)
            if result is None:
                continue

            if validate is not None and not validate(result.parsed):
                # Well-formed but unusable. Treated as a provider failure so the
                # chain tries the next one, and deliberately NOT cached.
                failures.append(Failure(
                    state.spec.name, Disposition.FALL_THROUGH,
                    "answer was rejected by the caller's validation",
                ))
                state.last_failure = failures[-1]
                continue

            if use_cache:
                self.cache.put(key, result.parsed.model_dump(mode="json"),
                               result.provider, result.model)
            return result

        raise AllProvidersFailed(failures)

    def _try(
        self, state: ProviderState, system: str, prompt: str,
        schema: type[BaseModel], tier: str, max_tokens: int,
        failures: list[Failure],
    ) -> Completion | None:
        client = self._client_for(state.spec)

        # At most two attempts: one retry only for a schema-validation miss,
        # where sampling variance genuinely might produce a valid answer.
        # Everything else moves on immediately.
        for attempt in (1, 2):
            state.calls += 1
            try:
                parsed, usage, model = client.complete(
                    system=system, prompt=prompt, schema=schema,
                    tier=tier, max_tokens=max_tokens,
                )
                state.successes += 1
                return Completion(parsed=parsed, provider=state.spec.name,
                                  model=model, usage=usage)
            except Exception as exc:  # noqa: BLE001
                failure = classify(state.spec.name, exc)
                state.last_failure = failure
                if failure.disposition is Disposition.DISABLE:
                    # Do not pay this provider's timeout again this process.
                    state.disabled = True
                    log.info("disabling %s for this run: %s",
                             state.spec.name, failure.reason)
                    failures.append(failure)
                    return None
                if failure.disposition is Disposition.RETRY and attempt == 1:
                    log.info("%s: %s — retrying once", state.spec.name, failure.reason)
                    continue
                failures.append(failure)
                return None
        return None

    def _client_for(self, spec: ProviderSpec):
        if spec.name not in self._clients:
            self._clients[spec.name] = (
                AnthropicClient(spec) if spec.protocol == "anthropic"
                else OpenAICompatClient(spec)
            )
        return self._clients[spec.name]


_default: LayeredClient | None = None


def client(order: str | list[str] | None = None) -> LayeredClient:
    """The process-wide chain.

    Shared so the circuit breaker means something: a provider disabled by one
    specialist stays disabled for the others, which is the entire point of
    remembering the failure.
    """
    global _default
    if _default is None or order is not None:
        from yukti.config import settings

        _default = LayeredClient(
            order=order if order is not None else settings().llm_providers,
            cache=cache_mod.ResponseCache(enabled=settings().llm_cache_enabled),
        )
    return _default


def reset() -> None:
    """Drop the shared chain. For tests, and after changing configuration."""
    global _default
    _default = None
