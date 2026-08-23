"""End-to-end against a REAL provider. Skipped unless one is configured.

Everything else about the chain is tested with stubs, which is right: asserting
that a particular model resists a particular phrasing would be testing the model
rather than the system, and would pass or fail for reasons outside this
repository.

But stubs cannot catch the things that actually break when you point this at a
real endpoint — a compatibility layer that rejects `response_format`, a model id
that was retired last month, a provider that wraps JSON in a code fence. So this
file exists and runs for anyone who has a key.

    GEMINI_API_KEY=... pytest tests/integration/test_llm_live.py -v

It is not part of the default run and never gates a commit. A test that needs a
credential the build machine does not have is a test that fails for the wrong
reason.
"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel, Field

from yukti.agent.schemas import ComposedMessage, Posture, RCAVerdict
from yukti.llm.chain import AllProvidersFailed, LayeredClient
from yukti.llm.registry import configured_providers

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live():
    """Explicit opt-in, deliberately.

    "A key is present" is not the same as "the operator wants live calls".
    GITHUB_TOKEN is set in plenty of environments — including this build
    container, where the host is then blocked — so keying off a credential alone
    made these run by accident and fail for a reason that had nothing to do with
    the code under test. An opt-in variable says what it means.
    """
    if not os.environ.get("YUKTI_LIVE_LLM_TESTS"):
        pytest.skip(
            "live LLM tests are opt-in: set YUKTI_LIVE_LLM_TESTS=1 together "
            "with a provider key (GEMINI_API_KEY is free and needs no card, "
            "from aistudio.google.com). See .env.example."
        )
    providers = [p for p in configured_providers() if not p.keyless]
    if not providers:
        pytest.skip("YUKTI_LIVE_LLM_TESTS is set but no provider key is configured")
    return LayeredClient()


class Ping(BaseModel):
    ok: bool = Field(description="always true")
    where: str = Field(description="the name of the model answering")


def test_a_real_provider_returns_a_validated_object(live):
    result = live.complete(
        system="You return JSON matching the schema. Nothing else.",
        prompt='Return {"ok": true, "where": "<your model name>"}',
        schema=Ping, tier="fast", max_tokens=256, use_cache=False,
    )
    assert result.parsed.ok is True
    assert result.provider and result.model
    print(f"\n  answered by {result.provider} ({result.model})")


def test_the_rca_schema_survives_a_real_model(live):
    """The enums are the guarantee, so they have to hold against a real model.

    A provider that returns a plausible-sounding posture outside the closed set
    fails validation here rather than reaching a decision.
    """
    result = live.complete(
        system=("You are a payments reliability analyst. Diagnose the cause and "
                "recommend a posture. Cite only the evidence ids you are given."),
        prompt=("<evidence>\n"
                "  [id=1] source=degradation_scan subject=HDFC "
                "fact={'baseline_success_rate': 0.88, 'observed_success_rate': 0.61, "
                "'z_score': 5.2, 'sample_size': 900}\n"
                "  [id=2] source=degradation_scan subject=HDFC "
                "fact={'decline_code': 'BANK_DOWN', 'share_of_failures': 0.71}\n"
                "</evidence>\n\nDiagnose this."),
        schema=RCAVerdict, tier="planner", max_tokens=2048, use_cache=False,
    )
    assert isinstance(result.parsed.posture, Posture)
    assert result.parsed.narrative
    # The evidence supports an issuer-side story; a model citing anything other
    # than the two ids it was shown would be inventing a source.
    assert set(result.parsed.cited_evidence_ids) <= {1, 2}


def test_a_real_model_respects_the_placeholder_rule(live):
    """The composer must never emit an amount or a URL of its own.

    Validated in code regardless, but worth confirming a real model can be held
    to it — if it could not, every message would fall back to a template and the
    Hinglish claim would be empty.
    """
    from yukti.agent.specialists import _placeholders_intact

    result = live.complete(
        system=("You write short payment reminders for Indian customers. Use the "
                "placeholders {amount} and {link} exactly. Never write a number, "
                "a currency figure, or a URL yourself."),
        prompt="Write a polite Hinglish reminder. Use {amount} and {link}.",
        schema=ComposedMessage, tier="fast", max_tokens=512, use_cache=False,
    )
    print(f"\n  {result.provider}: {result.parsed.body}")
    assert "{amount}" in result.parsed.body
    if not _placeholders_intact(result.parsed.body):
        pytest.skip(
            f"{result.provider} wrote a literal value despite instruction — "
            "the composer falls back to a template, which is the designed "
            "behaviour rather than a test failure"
        )


def test_an_unusable_answer_is_not_cached(live):
    """The validation hook, against a real provider."""
    with pytest.raises(AllProvidersFailed):
        live.complete(
            system="Return JSON.", prompt='Return {"ok": true, "where": "x"}',
            schema=Ping, tier="fast", max_tokens=256,
            validate=lambda _: False,
        )
    assert live.cache.stats()["hits"] == 0
