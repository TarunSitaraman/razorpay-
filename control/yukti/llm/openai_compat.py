"""One adapter for every provider that speaks OpenAI chat-completions.

Eight of the nine providers in the registry implement this wire format, so
there is one implementation and a table of base URLs rather than eight
integrations. The official `openai` SDK is used rather than hand-rolled HTTP:
it is the reference implementation of the protocol these vendors are
implementing, and rewriting it to save a dependency would mean re-deriving
error envelopes, retries and finish-reason handling that are already correct.

**Structured output degrades in three rungs**, because compatibility layers vary
in how much of the protocol they actually implement:

  1. `json_schema` — the response is constrained to the schema by the provider
  2. `json_object` — valid JSON is guaranteed, the shape is not
  3. prompt-only — the schema is described in the prompt and nothing is enforced

Every rung ends in Pydantic validation, so the capability flag is an
optimisation, not a correctness boundary. A provider that claims json_schema and
lies produces a validation error, which the chain treats as retry-then-move-on.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from yukti.llm.registry import ProviderSpec

log = logging.getLogger(__name__)

# Kept short on purpose. A provider that is blocked by an egress policy or is
# simply down should cost a couple of seconds before the chain moves on, not
# the SDK default. With up to eight providers configured, a long timeout turns
# a single decision into a minute of waiting.
CONNECT_TIMEOUT_S = 8.0
READ_TIMEOUT_S = 60.0


class OpenAICompatClient:
    """Talks to any OpenAI-compatible endpoint."""

    def __init__(self, spec: ProviderSpec) -> None:
        self.spec = spec
        self._client: Any = None

    def _lazy(self):
        if self._client is None:
            import httpx
            from openai import OpenAI

            self._client = OpenAI(
                # Keyless local runtimes still need a non-empty string; the
                # value is ignored by the server.
                api_key=self.spec.api_key() or "not-needed",
                base_url=self.spec.base_url,
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                # The chain is the retry mechanism. Letting the SDK retry too
                # would multiply the wait before the next provider is tried,
                # and a provider worth retrying is usually worth skipping.
                max_retries=0,
            )
        return self._client

    def complete(
        self, *, system: str, prompt: str, schema: type[BaseModel], tier: str,
        max_tokens: int = 4096,
    ) -> tuple[BaseModel, dict[str, int], str]:
        """Return (parsed, usage, model). Raises on failure; the chain classifies."""
        model = self.spec.model_for(tier)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        last: Exception | None = None
        for rung in self.spec.structured:
            try:
                return self._attempt(rung, model, messages, schema, max_tokens)
            except (ValidationError, ValueError) as exc:
                # The shape was wrong. Trying a weaker rung will not help, since
                # the weaker rungs constrain less — so this goes back to the
                # chain, which retries once and then changes provider.
                raise ValueError(f"{model} returned an unusable response: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                # A rejected PARAMETER is the interesting case: many
                # compatibility layers 400 on `response_format` while serving
                # ordinary requests perfectly well. Stepping down a rung costs
                # one request and rescues the provider entirely.
                if not _looks_like_unsupported_parameter(exc):
                    raise
                log.info("%s does not support %s; stepping down", self.spec.name, rung)
                last = exc
        raise last or RuntimeError(f"{self.spec.name}: no structured mode succeeded")

    def _attempt(
        self, rung: str, model: str, messages: list[dict], schema: type[BaseModel],
        max_tokens: int,
    ) -> tuple[BaseModel, dict[str, int], str]:
        client = self._lazy()

        if rung == "json_schema":
            completion = client.chat.completions.parse(
                model=model, messages=messages, max_tokens=max_tokens,
                response_format=schema,
            )
            parsed = completion.choices[0].message.parsed
            if parsed is None:
                raise ValueError("provider returned no parsed content")
            return parsed, _usage(completion), model

        if rung == "json_object":
            # The schema still has to reach the model — json_object guarantees
            # syntactically valid JSON and says nothing about its shape.
            messages = _with_schema_hint(messages, schema)
            completion = client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            return _validate(completion, schema), _usage(completion), model

        messages = _with_schema_hint(messages, schema)
        completion = client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens,
        )
        return _validate(completion, schema), _usage(completion), model


def _with_schema_hint(messages: list[dict], schema: type[BaseModel]) -> list[dict]:
    hint = (
        "\n\nRespond with a single JSON object and nothing else — no prose, no "
        "code fence. It must match this JSON schema exactly:\n"
        f"{json.dumps(schema.model_json_schema(), separators=(',', ':'))}"
    )
    out = [dict(m) for m in messages]
    out[-1]["content"] = out[-1]["content"] + hint
    return out


def _validate(completion: Any, schema: type[BaseModel]) -> BaseModel:
    content = completion.choices[0].message.content or ""
    return schema.model_validate_json(_strip_fence(content))


def _strip_fence(text: str) -> str:
    """Remove a markdown code fence.

    Smaller models wrap JSON in ```json fences however firmly they are told not
    to. Stripping it is a one-line accommodation of a universal habit; refusing
    would discard an otherwise perfectly good answer.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rsplit("```", 1)[0].strip()


def _usage(completion: Any) -> dict[str, int]:
    usage = getattr(completion, "usage", None)
    return {
        "input": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output": int(getattr(usage, "completion_tokens", 0) or 0),
    }


_UNSUPPORTED_MARKERS = (
    "response_format", "json_schema", "unsupported", "not supported",
    "unrecognized", "unknown parameter", "invalid_request_error",
)


def _looks_like_unsupported_parameter(exc: BaseException) -> bool:
    """Distinguish 'you sent a parameter I do not implement' from a real error.

    Matched on message text because compatibility layers do not agree on a
    machine-readable code for this. Deliberately narrow: a false positive costs
    one redundant request at a lower rung, while a false negative loses a
    provider that would have worked.
    """
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None)
    if status not in (400, 404, 422):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _UNSUPPORTED_MARKERS)
