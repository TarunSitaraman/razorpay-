"""Anthropic through its own SDK rather than a compatibility layer.

Kept separate for a reason that is not sentiment: `messages.parse` with a
Pydantic `output_format` validates server-side against the schema, which is a
stronger guarantee than any of the three rungs the OpenAI-compatible path can
offer. Routing Anthropic through the compatibility layer would discard that.

Adaptive thinking is used rather than a token budget — `budget_tokens` is
removed on the current model family and returns a 400. Depth is controlled by
`effort` instead.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from yukti.llm.registry import ProviderSpec

CONNECT_TIMEOUT_S = 8.0
READ_TIMEOUT_S = 120.0

# Thinking depth by tier. The high-volume path runs low: classifying a decline
# code or writing a reminder is not a reasoning problem, and paying for depth
# there is the fastest way to make the cost story embarrassing.
EFFORT_BY_TIER = {"fast": "low", "planner": "high"}


class AnthropicClient:
    def __init__(self, spec: ProviderSpec) -> None:
        self.spec = spec
        self._client: Any = None

    def _lazy(self):
        if self._client is None:
            import anthropic
            import httpx

            self._client = anthropic.Anthropic(
                api_key=self.spec.api_key(),
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                max_retries=0,   # the chain is the retry mechanism
            )
        return self._client

    def complete(
        self, *, system: str, prompt: str, schema: type[BaseModel], tier: str,
        max_tokens: int = 4096,
    ) -> tuple[BaseModel, dict[str, int], str]:
        model = self.spec.model_for(tier)
        response = self._lazy().messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT_BY_TIER.get(tier, "high")},
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        parsed = response.parsed_output
        if parsed is None:
            raise ValueError("anthropic returned no parsed output")
        usage = {
            "input": int(getattr(response.usage, "input_tokens", 0) or 0),
            "output": int(getattr(response.usage, "output_tokens", 0) or 0),
        }
        return parsed, usage, model
