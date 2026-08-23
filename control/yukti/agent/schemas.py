"""Structured outputs. Every model response lands in one of these.

Each field is either a closed enum or a plain string that no downstream code
treats as authoritative. Nothing here carries a number that reaches a customer
or a ledger: amounts, discounts, timings and policy verdicts are all computed
elsewhere, and a model that tried to supply one would find no field to put it in.

That is the point of writing the schemas first. The clamp is structural — a
value outside the enum fails validation rather than flowing into a decision.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Posture(StrEnum):
    """What to do about a degradation, as a closed set.

    Recommendations are clamped to these four because an LLM asked for a course
    of action in free text will eventually produce one nobody implemented.
    """

    SUPPRESS_AND_WAIT = "suppress_and_wait"        # issuer-side, transient
    SILENT_RETRY_LATER = "silent_retry_later"      # recoverable without contact
    ROUTE_AROUND = "route_around"                  # try a different rail or PSP
    NO_ACTION_NEEDED = "no_action_needed"          # not a real degradation


class RootCause(StrEnum):
    ISSUER_OUTAGE = "issuer_outage"
    PSP_DEGRADATION = "psp_degradation"
    FUNDS_CYCLE = "funds_cycle"                    # salary-cycle, not a fault
    AUTH_FRICTION = "auth_friction"                # OTP/PIN failures rising
    MANDATE_ATTRITION = "mandate_attrition"        # revocations rising
    UNCLEAR = "unclear"                            # the honest default


class RCAVerdict(BaseModel):
    """Root-cause analysis of one degradation signal."""

    root_cause: RootCause
    posture: Posture
    # One or two sentences a merchant would actually read. No numbers the
    # evidence did not contain.
    narrative: str = Field(max_length=600)
    # Evidence row ids this conclusion rests on. Checked against what was
    # supplied; a citation to an id that was not shown is a fabricated source.
    cited_evidence_ids: list[int] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class CohortStrategy(BaseModel):
    """A proposal for how to work a cohort.

    `deprioritise` is the only field with teeth, and it can only ever REMOVE
    candidates. A planner that wanted to add an action it was not offered has
    nowhere to say so, which is what keeps the LLM layer non-load-bearing: the
    worst a wrong or injected plan achieves is that Yukti considers fewer
    options.
    """

    # Action kinds to drop for this cohort, by value. Validated against
    # ActionKind at the call site; unknown members are ignored rather than
    # raising, so a hallucinated action name is inert.
    deprioritise: list[str] = Field(default_factory=list)
    rationale: str = Field(max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)


class ComposedMessage(BaseModel):
    """Channel copy.

    `body` may contain the placeholders the template registers and nothing else.
    Amounts and links are injected by code after validation — the model never
    sees a rupee figure it could get wrong, and never produces a URL.
    """

    body: str = Field(max_length=640)
    # Hinglish/code-mixed is a named track direction and is genuinely hard to
    # template, which is why it is a model job at all.
    language: str = Field(default="en")
