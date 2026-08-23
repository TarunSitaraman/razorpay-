"""Randomised exploration history — the RCT that makes uplift identifiable.

An uplift model needs (features, treatment, outcome) triples in which treatment
was assigned **independently of the outcome**. Without that, the treatment
effect is not identified and the model simply relearns whatever policy chose
the actions. This is not a synthetic-data quirk: it is the reason real dunning
teams must run a holdout and spend an explicit exploration budget before they
can claim any lift at all.

So this module simulates a past period in which recovery actions were assigned
uniformly at random — random action kind, channel, hour, discount tier — or
withheld entirely for a control group. Outcomes come from the same oracle the
evaluation harness uses, so training data and evaluation share one model of the
world.

What makes it a valid RCT, and what the tests assert:
  * treatment assignment is drawn from a fixed distribution that reads NOTHING
    about the customer — not the archetype, not the history, not the amount;
  * a control group receives no action at all, so the untreated outcome is
    observed rather than imputed;
  * assignment is seeded, so the whole history reproduces exactly on replay.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from yukti.domain.enums import ActionKind, Channel, UpliftArchetype

from yukti_datagen.calendar import DowntimeWindow
from yukti_datagen.response import CaseContext, Intervention, evaluate

# The exploration policy. Uniform-ish over a bounded action space, and
# deliberately independent of every customer attribute — that independence IS
# the identification strategy.
EXPLORE_ACTIONS: dict[ActionKind, float] = {
    ActionKind.SUPPRESS: 0.25,        # the control arm
    ActionKind.MESSAGE: 0.30,
    ActionKind.SILENT_RETRY: 0.15,
    ActionKind.SCHEDULE_DEBIT: 0.10,
    ActionKind.DISCOUNT_OFFER: 0.12,
    ActionKind.PAYMENT_LINK: 0.05,
    ActionKind.VOICE_CALL: 0.03,
}

EXPLORE_CHANNELS: dict[Channel, float] = {
    Channel.WHATSAPP: 0.40,
    Channel.SMS: 0.30,
    Channel.EMAIL: 0.25,
    Channel.VOICE: 0.05,
}

EXPLORE_DISCOUNTS: tuple[float, ...] = (0.0, 5.0, 10.0, 15.0, 25.0)

# Per-channel cost the merchant actually pays, in paise. Real numbers matter:
# the allocator optimises net margin, so a voice call that costs more than it
# recovers must be visibly unprofitable in the training data.
CHANNEL_COST_PAISE: dict[Channel, int] = {
    Channel.NONE: 0,
    Channel.EMAIL: 10,
    Channel.SMS: 25,
    Channel.WHATSAPP: 75,
    Channel.VOICE: 900,
}


def _weighted(rng: random.Random, options: dict) -> object:
    total = sum(options.values())
    r = rng.random() * total
    upto = 0.0
    for value, weight in options.items():
        upto += weight
        if r <= upto:
            return value
    return next(iter(options))


@dataclass(slots=True)
class HistoricalTreatment:
    """One randomised intervention and the outcome the oracle returned."""

    case_id: str
    obligation_id: str
    merchant_id: str
    customer_id: str
    action_kind: str
    channel: str
    scheduled_for: datetime
    discount_pct: float
    cost_paise: int
    # What the offer was worth. Describes the intervention, so it is safe as a
    # feature and is what the allocator prices against.
    discount_paise: int
    # What the merchant actually gave away — zero unless the case recovered.
    # An OUTCOME, never a feature; used only for realised-margin accounting.
    realised_discount_paise: int
    recovered: bool
    opted_out: bool
    recovered_paise: int
    # Ground truth, recorded for scoring only. Never a feature.
    archetype: str
    true_p_recover: float


def sample_intervention(rng: random.Random, at: datetime) -> Intervention:
    """Draw an action with no reference to the customer.

    Everything about this draw is independent of who is being treated. That
    independence is what makes the resulting dataset an RCT rather than an
    observational log.
    """
    kind = _weighted(rng, EXPLORE_ACTIONS)

    if kind.contacts_customer or kind is ActionKind.PAYMENT_LINK:
        channel = _weighted(rng, EXPLORE_CHANNELS)
    else:
        channel = Channel.NONE

    discount = _weighted(
        rng, dict.fromkeys(EXPLORE_DISCOUNTS, 1.0)
    ) if kind is ActionKind.DISCOUNT_OFFER else 0.0

    # Random hour across the full day, NOT clipped to TRAI hours. The policy
    # engine enforces the 09:00-21:00 window at decision time; if exploration
    # never sampled outside it, the model could never learn what those hours
    # are worth and the constraint would look free.
    hour = rng.randint(0, 23)
    minute = rng.randint(0, 59)
    scheduled = at.replace(hour=hour, minute=minute, second=0, microsecond=0)

    return Intervention(kind=kind, channel=channel, at=scheduled, discount_pct=discount)


def run_exploration(
    cases: list[dict],
    customers: dict[str, dict],
    downtime: list[DowntimeWindow],
    seed: int,
) -> list[HistoricalTreatment]:
    """Apply one randomised intervention to each case and record the outcome.

    ``cases`` rows carry obligation/customer identifiers, the failure context
    and a timestamp. ``customers`` maps customer_id to attributes including the
    ground-truth archetype the oracle needs (and the model must never see).
    """
    rng = random.Random(seed)
    out: list[HistoricalTreatment] = []

    # Contacts already made per customer in the trailing week, so fatigue
    # accumulates realistically across the exploration period.
    contact_log: dict[str, list[datetime]] = {}

    for case in sorted(cases, key=lambda c: c["ts"]):
        cust = customers.get(case["customer_id"])
        if cust is None:
            continue

        at = case["ts"]
        iv = sample_intervention(rng, at)

        recent = contact_log.setdefault(case["customer_id"], [])
        cutoff = at - timedelta(days=7)
        prior_contacts = sum(1 for t in recent if t >= cutoff)

        in_downtime = any(
            w.issuer == cust["issuer"] and w.covers(at) for w in downtime
        )

        ctx = CaseContext(
            case_id=case["case_id"],
            archetype=UpliftArchetype(cust["archetype"]),
            amount_paise=case["amount_paise"],
            decline_code=case["decline_code"] or "UNKNOWN",
            rail_is_mandate=case["rail_is_mandate"],
            preferred_channel=Channel(cust["preferred_channel"]),
            prior_contacts_7d=prior_contacts,
            # Read from the case rather than hardcoded. The oracle has always
            # modelled promises — an open one floors organic recovery and
            # chasing through it costs 18 points — but passing False here meant
            # no training example ever exhibited the effect, so the uplift model
            # could not learn it and the stopping rule could not be validated.
            open_promise=bool(case.get("open_promise", False)),
            in_downtime=in_downtime,
        )
        outcome = evaluate(ctx, iv, seed)

        if iv.kind.contacts_customer:
            recent.append(at)

        # What was OFFERED, not what was ultimately given away.
        #
        # This previously recorded the discount only when the case recovered,
        # which is economically true — a discount costs nothing if the customer
        # never pays — and catastrophic as a feature. `discount_paise > 0` then
        # predicted recovery with 100% accuracy (1,936 of 1,936 rows), so any
        # model allowed to see the column learned the outcome instead of the
        # effect.
        #
        # It hid for two days because TLearner and XLearner strip the action
        # columns from both arms. Only ActionConditionalUplift keeps them — that
        # is the entire point of it — and it found the leak immediately, scoring
        # a 5% discount at +0.92 uplift while every other action scored negative.
        #
        # The offered amount is also the right number for the decision: the
        # allocator subtracts discount in full because it must commit the money
        # before knowing whether it lands. Realised spend is an accounting
        # question for the evaluation, computed from the outcome, not a feature.
        offered_discount_paise = (
            int(round(case["amount_paise"] * iv.discount_pct / 100.0))
            if iv.discount_pct else 0
        )
        realised_discount_paise = (
            offered_discount_paise if outcome.recovered else 0
        )

        out.append(
            HistoricalTreatment(
                case_id=case["case_id"],
                obligation_id=case["obligation_id"],
                merchant_id=case["merchant_id"],
                customer_id=case["customer_id"],
                action_kind=iv.kind.value,
                channel=iv.channel.value,
                scheduled_for=iv.at,
                discount_pct=iv.discount_pct,
                cost_paise=CHANNEL_COST_PAISE[iv.channel] if iv.kind.contacts_customer else 0,
                discount_paise=offered_discount_paise,
                realised_discount_paise=realised_discount_paise,
                recovered=outcome.recovered,
                opted_out=outcome.opted_out,
                recovered_paise=outcome.recovered_paise,
                archetype=cust["archetype"],
                true_p_recover=outcome.p_recover,
            )
        )
    return out
