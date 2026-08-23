"""Promise-to-pay generation.

The track brief names a "promise-to-pay tracker" as one of its directions, and
the outcome oracle already models promises correctly: an unbroken promise floors
organic recovery around 0.60, and chasing through one is measurably negative
because it reads as distrust. But nothing was ever creating promises, so
`StopReason.OPEN_PROMISE_TO_PAY` was a rule that could not fire and the effect
the oracle models was unobservable in training data.

Promises are generated here rather than inside the exploration loop for one
reason: they must exist on BOTH sides of the exploration/planning cutoff. On the
exploration side they give the uplift model examples of chasing through a
promise and losing; on the planning side they give the stopping rules a live
case to stop, which is demo beat 7.

They are a B2B phenomenon first. A receivables clerk who says "we'll pay on the
5th" is routine; a cart abandoner promising to come back is not, and the rates
here reflect that asymmetry rather than spreading promises evenly for
convenience.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from yukti.domain.enums import PromiseState
from yukti.domain.ids import promise_id

# P(a promise is elicited) by obligation kind. Invoices dominate: the whole
# point of a receivables chase is to extract a commitment to a date.
PROMISE_RATE: dict[str, float] = {
    "invoice": 0.28,
    "subscription_cycle": 0.06,
    "order": 0.04,
    "cart": 0.01,
}

# How the promise reached us. Confidence differs by source because a merchant
# typing it into their dashboard is a fact, while inferring one from a reply is
# a guess — and the stopping rule should be able to treat them differently.
SOURCES: dict[str, tuple[float, float]] = {
    # source: (share, confidence)
    "customer_reply": (0.55, 0.80),
    "merchant_entry": (0.25, 0.92),
    "voice_call":     (0.12, 0.75),
    "inferred":       (0.08, 0.45),
}

# Share of matured promises that were actually kept. Consistent with the
# industry figure the plan cites, and it matters: if promises were always kept
# the stopping rule would be free, and if they were never kept it would be
# indefensible. The interesting case is the one where it is a real bet.
KEPT_RATE = 0.60


@dataclass(frozen=True, slots=True)
class Promise:
    id: str
    obligation_id: str
    promised_amount_paise: int
    promised_for: datetime
    source: str
    state: str
    confidence: float
    created_at: datetime
    resolved_at: datetime | None


def _weighted_source(rng: random.Random) -> str:
    r = rng.random()
    upto = 0.0
    for name, (share, _) in SOURCES.items():
        upto += share
        if r <= upto:
            return name
    return "customer_reply"


def generate_promises(
    seed: int, obligations: list[dict], failed_at: dict[str, datetime], now: datetime
) -> list[Promise]:
    """One promise for a share of unpaid obligations.

    `now` is the end of the simulated timeline. A promise whose date has not yet
    arrived is `open` — which is exactly the state that stops a chase. One that
    has matured is resolved to kept or broken, and a broken promise deliberately
    leaves the obligation chaseable again: a customer who has already broken
    their word is the one case where following up is the right call.
    """
    rng = random.Random(seed ^ 0x9E3779B9)
    out: list[Promise] = []

    for o in obligations:
        if o["state"] != "open":
            continue
        rate = PROMISE_RATE.get(o["kind"], 0.0)
        if rng.random() >= rate:
            continue
        origin = failed_at.get(o["id"])
        if origin is None:
            continue

        # When the promise was made, relative to the failure being recovered.
        #
        # A cart abandoner can only promise after we chase them, so the offset
        # is positive. A B2B customer is different: they promise against an
        # ageing balance ("we'll clear this on the 5th") and the auto-debit
        # fails afterwards, so the promise predates the failure. Subscriptions
        # behave the same way — "I'll sort the card next month" comes before
        # next month's charge declines.
        #
        # This matters beyond realism. If every promise followed its failure,
        # no case would ever be evaluated with a promise already open, and the
        # one thing the promise machinery exists to test — that we stay quiet
        # when someone has already committed to a date — would never be exercised.
        if o["kind"] in ("invoice", "subscription_cycle") and rng.random() < 0.45:
            offset = timedelta(days=-rng.randint(2, 10), hours=rng.randint(0, 23))
        else:
            offset = timedelta(days=rng.randint(1, 6), hours=rng.randint(0, 23))
        created = origin + offset
        if created >= now:
            continue
        promised_for = created + timedelta(days=rng.randint(3, 21))

        source = _weighted_source(rng)
        confidence = SOURCES[source][1]

        # Partial promises are real in B2B — "we can clear half this week".
        amount = o["amount_paise"]
        if o["kind"] == "invoice" and rng.random() < 0.22:
            amount = max(1, int(amount * rng.choice([0.3, 0.4, 0.5, 0.6])))

        if promised_for > now:
            state, resolved_at = PromiseState.OPEN.value, None
        elif rng.random() < KEPT_RATE:
            state, resolved_at = PromiseState.KEPT.value, promised_for
        else:
            state, resolved_at = PromiseState.BROKEN.value, promised_for

        out.append(Promise(
            id=promise_id(), obligation_id=o["id"], promised_amount_paise=amount,
            promised_for=promised_for, source=source, state=state,
            confidence=confidence, created_at=created, resolved_at=resolved_at,
        ))

    return out
