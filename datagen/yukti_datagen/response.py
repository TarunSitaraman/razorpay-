"""The outcome oracle: a potential-outcomes model of customer recovery.

This is the intellectual centre of the evaluation. It is NOT a static dataset —
it is an environment that answers counterfactuals. Given the same case, it can
say what happens under no action, under a silent retry, under an SMS at 19:00,
or under a 10% discount on WhatsApp. That is what lets every baseline arm and
Yukti be scored on *identical* cases, which is the only way a lift number means
anything.

The model is the classic uplift quadrant, made concrete for Indian payments:

    SURE THING    recovers anyway              -> treating is pure cost
    PERSUADABLE   recovers only if treated     -> the only profitable target
    LOST CAUSE    never recovers               -> treating is pure cost
    SLEEPING DOG  recovers if left alone,      -> treating is NEGATIVE value
                  churns if over-contacted

A propensity model ranks Sure Things at the top, because they do have the
highest P(recover | treated). An uplift model ranks Persuadables at the top.
That divergence is the entire thesis, and it is baked in here deliberately.

Determinism: every draw is a hash of (seed, case_id, purpose), so the oracle
returns the same answer for the same question no matter how many times, or in
what order, arms ask it.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime

from yukti.domain.decline import lookup
from yukti.domain.enums import (
    ActionKind,
    Channel,
    Transience,
    UpliftArchetype,
)

from yukti_datagen.calendar import (
    balance_availability,
    hour_conversion_multiplier,
)


def _draw(seed: int, *parts: object) -> float:
    key = "|".join([str(seed), *(str(p) for p in parts)])
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big") / 2**64


# Baseline probability that the obligation resolves on its own within the
# attribution window, with no intervention at all. This is what the holdout arm
# measures, and what every "we recovered X" claim in this industry omits.
ORGANIC_RECOVERY: dict[UpliftArchetype, float] = {
    UpliftArchetype.SURE_THING: 0.72,
    UpliftArchetype.PERSUADABLE: 0.09,
    UpliftArchetype.LOST_CAUSE: 0.01,
    UpliftArchetype.SLEEPING_DOG: 0.46,
}

# Ceiling on how much a *perfectly chosen* intervention can add, in absolute
# probability. Persuadables are the only archetype where this is large.
MAX_UPLIFT: dict[UpliftArchetype, float] = {
    UpliftArchetype.SURE_THING: 0.04,     # small; they were going to pay anyway
    UpliftArchetype.PERSUADABLE: 0.46,
    UpliftArchetype.LOST_CAUSE: 0.01,
    UpliftArchetype.SLEEPING_DOG: 0.05,
}

# Probability that a contact causes an opt-out / churn, per contact beyond the
# archetype's tolerance. Sleeping dogs are the ones this destroys.
IRRITATION: dict[UpliftArchetype, float] = {
    UpliftArchetype.SURE_THING: 0.010,
    UpliftArchetype.PERSUADABLE: 0.015,
    UpliftArchetype.LOST_CAUSE: 0.020,
    UpliftArchetype.SLEEPING_DOG: 0.115,   # an order of magnitude worse
}

# How many contacts an archetype tolerates before irritation starts biting.
CONTACT_TOLERANCE: dict[UpliftArchetype, int] = {
    UpliftArchetype.SURE_THING: 2,
    UpliftArchetype.PERSUADABLE: 3,
    UpliftArchetype.LOST_CAUSE: 2,
    UpliftArchetype.SLEEPING_DOG: 0,       # any contact is already too many
}

# Channel effectiveness, before per-customer preference is applied.
CHANNEL_POWER: dict[Channel, float] = {
    Channel.NONE: 0.0,
    Channel.EMAIL: 0.55,
    Channel.SMS: 0.75,
    Channel.WHATSAPP: 1.00,
    Channel.VOICE: 1.15,
}


@dataclass(frozen=True, slots=True)
class CaseContext:
    """Everything the oracle needs to evaluate an intervention on one case."""

    case_id: str
    archetype: UpliftArchetype
    amount_paise: int
    decline_code: str
    rail_is_mandate: bool
    preferred_channel: Channel
    # Contacts this customer has already received in the trailing 7 days, from
    # ALL sources. Cross-agent fatigue is a property of the customer, not of any
    # one agent — which is exactly why a per-agent budget cannot see it.
    prior_contacts_7d: int
    # Whether an unbroken promise-to-pay is outstanding on this obligation.
    open_promise: bool
    in_downtime: bool


@dataclass(frozen=True, slots=True)
class Intervention:
    """A candidate action, evaluated counterfactually."""

    kind: ActionKind
    channel: Channel = Channel.NONE
    at: datetime | None = None
    discount_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class Outcome:
    recovered: bool
    opted_out: bool
    p_recover: float          # the probability that was realised against
    uplift: float             # p_recover - p_organic, the true causal effect
    recovered_paise: int


def _timing_quality(ctx: CaseContext, iv: Intervention) -> float:
    """How good the *timing* of this action is, as a quality factor in [0, 1].

    Quality factors are normalised so that 1.0 means "the best this action could
    possibly be timed". That keeps MAX_UPLIFT an actual ceiling: effect is
    headroom x quality, so a perfectly executed intervention reaches the
    archetype's headroom and never exceeds it. An earlier version let these
    compound above 1.0, which pushed persuadables past sure things on raw
    P(recover) and destroyed the propensity/uplift divergence the evaluation
    depends on.

    Two distinct mechanisms, deliberately not merged:
      * money-moving actions depend on whether the customer has funds, which is
        periodic around salary credit;
      * contact actions depend on whether the customer is awake and receptive.
    A system that models only one of these gets half the available lift.
    """
    if iv.at is None:
        return 0.7          # unscheduled: assume mediocre timing
    spec = lookup(ctx.decline_code)

    if iv.kind.moves_money:
        if spec.transience is Transience.TRANSIENT_FUNDS:
            # The salary-day signal the debit-timing model must discover.
            # balance_availability spans ~0.45..1.35; normalise to its peak.
            return min(1.0, balance_availability(iv.at.day) / 1.35)
        if spec.transience is Transience.TRANSIENT_SYSTEM:
            # Retrying into an ongoing outage fails; waiting it out succeeds.
            return 0.10 if ctx.in_downtime else 1.0
        return 0.85

    if iv.kind.contacts_customer:
        # hour curve spans 0.10..1.30; normalise to its peak.
        q = hour_conversion_multiplier(iv.at.hour) / 1.30
        if ctx.in_downtime:
            # Nudging a customer to pay while the issuer is down produces a
            # second failure and a worse impression.
            q *= 0.35
        return min(1.0, q)
    return 0.85


def _fit_quality(ctx: CaseContext, iv: Intervention) -> float:
    """How well the action suits the failure and the customer, in [0, 1]."""
    spec = lookup(ctx.decline_code)
    q = 1.0

    if iv.kind.contacts_customer:
        q *= CHANNEL_POWER[iv.channel] / CHANNEL_POWER[Channel.VOICE]
        if iv.channel is not ctx.preferred_channel:
            q *= 0.80
        if not spec.customer_actionable:
            # Contacting about a pure system failure cannot help: there is
            # nothing the customer can do about it.
            q *= 0.20
        # A bare nudge leaves room for an incentive to add something on top.
        q *= 0.70

    if iv.kind.moves_money:
        if not spec.retryable_silently:
            q *= 0.15          # e.g. an expired card: no retry count helps
        if spec.transience is Transience.PERMANENT:
            return 0.0         # revoked mandate: strictly zero, never recoverable

    if iv.kind is ActionKind.DISCOUNT_OFFER:
        # Diminishing returns: the first 10% moves people, the next 20% mostly
        # transfers margin away. sqrt gives that shape without a magic table.
        q *= 0.70 + 0.43 * math.sqrt(max(0.0, iv.discount_pct) / 100.0)

    # High-value obligations convert less readily on a single nudge.
    if ctx.amount_paise > 5_000_00:
        q *= 0.88
    return max(0.0, min(1.0, q))


def _fatigue_quality(ctx: CaseContext, iv: Intervention) -> float:
    """Response decay from prior contacts, across every agent.

    Multiplicative decay per prior contact in the trailing week. This is the
    term that makes cross-agent arbitration worth money: three agents each
    sending one "reasonable" message produce a third contact worth ~0.5 of the
    first, while consuming three times the budget. Because fatigue is a property
    of the *customer*, no per-agent budget can observe it.
    """
    if not iv.kind.contacts_customer:
        return 1.0
    return 0.78**ctx.prior_contacts_7d


def evaluate(ctx: CaseContext, iv: Intervention, seed: int) -> Outcome:
    """Counterfactual outcome of applying ``iv`` to ``ctx``.

    Determinism note: the recovery draw is keyed on the case only, NOT on the
    intervention. That is deliberate and important — it means the same customer
    "would have paid at threshold u" regardless of which arm is being scored, so
    two arms differ only through the probability they induce, not through luck.
    This is the paired-comparison property that makes small lift measurable
    without enormous sample sizes.
    """
    p_organic = ORGANIC_RECOVERY[ctx.archetype]

    # An unbroken promise-to-pay is itself strong evidence of intent: roughly
    # three in five are kept. So the baseline is already high WITHOUT any chase,
    # which is precisely what makes chasing through one a losing move — there is
    # very little headroom left to win, and real damage available to do.
    if ctx.open_promise:
        p_organic = max(p_organic, 0.60)

    if iv.kind in (ActionKind.SUPPRESS, ActionKind.ESCALATE):
        p = p_organic
    else:
        headroom = MAX_UPLIFT[ctx.archetype]
        # Quality factors are each in [0, 1], so effect is bounded by headroom.
        quality = (
            _timing_quality(ctx, iv)
            * _fit_quality(ctx, iv)
            * _fatigue_quality(ctx, iv)
        )
        effect = headroom * quality

        # Chasing through an unbroken promise reads as distrust and measurably
        # lowers the chance it is kept. Against an already-high baseline this
        # makes the net effect negative, which is what earns OPEN_PROMISE_TO_PAY
        # its place as a stopping rule rather than a courtesy.
        if ctx.open_promise and iv.kind.contacts_customer:
            effect = -0.18

        # Sleeping dogs: contact suppresses rather than helps.
        if ctx.archetype is UpliftArchetype.SLEEPING_DOG and iv.kind.contacts_customer:
            effect = -0.14 * CHANNEL_POWER[iv.channel]

        p = max(0.0, min(0.985, p_organic + effect))

    # Paired draw: fixed per case, so arms are compared on the same customer.
    u = _draw(seed, "recover", ctx.case_id)
    recovered = u < p

    opted_out = False
    if iv.kind.contacts_customer:
        excess = max(0, ctx.prior_contacts_7d + 1 - CONTACT_TOLERANCE[ctx.archetype])
        if excess > 0:
            p_out = 1.0 - (1.0 - IRRITATION[ctx.archetype]) ** excess
            opted_out = _draw(seed, "optout", ctx.case_id, ctx.prior_contacts_7d) < p_out
            if opted_out:
                recovered = False

    net = ctx.amount_paise
    if recovered and iv.discount_pct > 0:
        net -= int(round(ctx.amount_paise * iv.discount_pct / 100.0))

    return Outcome(
        recovered=recovered,
        opted_out=opted_out,
        p_recover=p,
        uplift=p - p_organic,
        recovered_paise=net if recovered else 0,
    )


def true_uplift(ctx: CaseContext, iv: Intervention) -> float:
    """Ground-truth causal effect of an intervention, with no sampling noise.

    Used only by the evaluation harness to report how close each arm got to the
    achievable optimum. No model may read this.
    """
    return evaluate(ctx, iv, seed=0).uplift
