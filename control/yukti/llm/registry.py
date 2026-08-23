"""Known LLM providers, and what it takes to reach each one.

Every provider here except Anthropic speaks the OpenAI chat-completions wire
format, which is why there is one adapter and a table rather than eight
integrations. Anthropic keeps its own path because its SDK is already in use and
its structured-output surface is better than the compatibility layer.

**Model ids drift.** The defaults below were correct when written and will not
stay correct — providers rename and retire models constantly. Every one is
overridable per provider via environment variable (`YUKTI_GROQ_MODEL_FAST` and
so on), and a wrong id produces a clean fall-through to the next provider rather
than a failure, so a stale default costs a retry and not an outage.

**Rate limits are documented for ORDERING ONLY, and are approximate.** They are
not enforced or predicted anywhere in the code. Correctness comes from handling
the 429 that actually arrives, which requires no table to be accurate — a
principle worth keeping, because a hardcoded limit that drifts out of date fails
in the direction of pretending a provider is available when it is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """How to reach one provider, and what it can do."""

    name: str
    base_url: str | None            # None = the SDK's own default (Anthropic)
    # Environment variables holding the key, tried in order. Several providers
    # are commonly configured under more than one name.
    key_env: tuple[str, ...]
    default_fast: str
    default_planner: str
    # Wire protocol. Everything except Anthropic is OpenAI-compatible.
    protocol: str = "openai"
    # Structured-output capability, best first. The adapter degrades along this
    # list: a provider that rejects json_schema is retried with json_object,
    # and one that rejects both gets the schema in the prompt. All three paths
    # validate with Pydantic afterwards, so the capability flag is an
    # optimisation rather than a correctness boundary.
    structured: tuple[str, ...] = ("json_schema", "json_object", "prompt")
    # Free-tier guidance. Approximate, for ordering only. See module docstring.
    free_tier: str = ""
    notes: str = ""
    # True when no key is needed at all (a local runtime).
    keyless: bool = False

    def api_key(self) -> str | None:
        for var in self.key_env:
            value = os.environ.get(var)
            if value and value.strip():
                return value.strip()
        return None

    def configured(self) -> bool:
        return self.keyless or self.api_key() is not None

    def model_for(self, tier: str) -> str:
        """Resolve the model id, honouring a per-provider override."""
        override = os.environ.get(f"YUKTI_{self.name.upper()}_MODEL_{tier.upper()}")
        if override:
            return override.strip()
        return self.default_fast if tier == "fast" else self.default_planner


# Ordered by free-tier generosity and speed, which is the default chain order.
# Gemini leads because it is the one provider verified reachable from the build
# environment; the rest are there for anyone running this anywhere else.
PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        default_fast="gemini-2.0-flash",
        default_planner="gemini-2.0-flash",
        free_tier="~15 RPM / ~1500 RPD on the free AI Studio tier",
        notes="Free key from aistudio.google.com, no card. The only external "
              "LLM host reachable from this build environment.",
    ),
    ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        key_env=("GROQ_API_KEY",),
        default_fast="llama-3.1-8b-instant",
        default_planner="llama-3.3-70b-versatile",
        free_tier="~30 RPM / ~14k RPD free",
        notes="Fastest free inference available. Blocked by egress policy in "
              "this build environment; works anywhere else.",
    ),
    ProviderSpec(
        name="cerebras",
        base_url="https://api.cerebras.ai/v1",
        key_env=("CEREBRAS_API_KEY",),
        default_fast="llama3.1-8b",
        default_planner="llama-3.3-70b",
        free_tier="~30 RPM free",
    ),
    ProviderSpec(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        key_env=("OPENROUTER_API_KEY",),
        # The `:free` suffix is what selects a no-cost model on OpenRouter.
        default_fast="meta-llama/llama-3.3-70b-instruct:free",
        default_planner="meta-llama/llama-3.3-70b-instruct:free",
        free_tier="~20 RPM on :free models; daily cap depends on account age",
        notes="Aggregates many providers behind one key.",
    ),
    ProviderSpec(
        name="mistral",
        base_url="https://api.mistral.ai/v1",
        key_env=("MISTRAL_API_KEY",),
        default_fast="mistral-small-latest",
        default_planner="mistral-large-latest",
        free_tier="~1 RPS on the free experiment tier",
    ),
    ProviderSpec(
        name="github",
        base_url="https://models.inference.ai.azure.com",
        key_env=("GITHUB_TOKEN", "GITHUB_MODELS_TOKEN"),
        default_fast="gpt-4o-mini",
        default_planner="gpt-4o",
        free_tier="low RPM, tied to a GitHub account",
        notes="Uses an ordinary GitHub PAT — often the key someone already has.",
    ),
    ProviderSpec(
        name="together",
        base_url="https://api.together.xyz/v1",
        key_env=("TOGETHER_API_KEY",),
        default_fast="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        default_planner="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        free_tier="limited free models",
    ),
    ProviderSpec(
        name="ollama",
        base_url="http://localhost:11434/v1",
        key_env=(),
        keyless=True,
        default_fast="llama3.2",
        default_planner="llama3.1",
        # Older local builds often reject json_schema; json_object is the safe
        # first rung and costs nothing when the newer path would have worked.
        structured=("json_object", "prompt"),
        free_tier="unlimited — runs on your own machine",
        notes="Fully local, no key, no network. Nothing leaves the machine, "
              "which is the only option that satisfies a strict data boundary.",
    ),
    ProviderSpec(
        name="anthropic",
        base_url=None,
        key_env=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        default_fast="claude-haiku-4-5",
        default_planner="claude-opus-5",
        protocol="anthropic",
        structured=("native",),
        free_tier="paid — no free tier",
        notes="Not free, but the best structured-output surface. Last in the "
              "default chain so it is used only when nothing free is configured.",
    ),
)

BY_NAME: dict[str, ProviderSpec] = {p.name: p for p in PROVIDERS}

DEFAULT_ORDER: tuple[str, ...] = tuple(p.name for p in PROVIDERS)


def resolve_order(names: str | list[str] | None) -> list[ProviderSpec]:
    """Turn a configured provider order into specs, ignoring unknown names.

    Unknown names are dropped rather than raising: a typo in an env var should
    cost one provider, not the whole run. Nothing here checks for a key —
    that is the chain's job, and it wants to report "configured: no" rather
    than silently omit a provider the operator believes is in play.
    """
    # An empty string and None both mean "the default order". They arrive from
    # different places — an unset env var and an omitted argument — and treating
    # the empty string as "no providers" would silently disable the whole chain
    # for anyone who had merely left the setting blank.
    if names is None or (isinstance(names, str) and not names.strip()):
        chosen = list(DEFAULT_ORDER)
    elif isinstance(names, str):
        chosen = [n.strip() for n in names.split(",") if n.strip()]
    else:
        chosen = list(names) or list(DEFAULT_ORDER)
    return [BY_NAME[n] for n in chosen if n in BY_NAME]


def configured_providers(order: list[ProviderSpec] | None = None) -> list[ProviderSpec]:
    return [p for p in (order or list(PROVIDERS)) if p.configured()]
