"""Generate the 90-day synthetic world and its event stream.

Output is two things:
  1. Reference data in Postgres (merchants, customers, obligations, attempts) —
     what the control plane reads.
  2. A Parquet event log — what gets replayed into Kafka, in timestamp order,
     shaped exactly like the webhooks the sandbox emits.

The generator produces the world and the *failures*. It does not decide what
happens after an intervention: that is the outcome oracle's job, asked at
evaluation time. Keeping the two apart is what makes counterfactual comparison
possible at all.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from yukti.domain.enums import ObligationKind, Rail
from yukti.domain.ids import attempt_id, event_id, obligation_id

from yukti_datagen.calendar import (
    DegradationEpisode,
    DowntimeWindow,
    balance_availability,
    generate_degradation_episodes,
    generate_downtime_windows,
    is_weekend,
)
from yukti_datagen.promises import Promise, generate_promises
from yukti_datagen.world import ISSUERS, PSPS, Merchant, MerchantSpec, _weighted, build_world

# Base per-rail success rate before any environment effect. UPI intent
# meaningfully outperforms UPI collect, which is a real and well-documented
# asymmetry on Indian rails and one the planner should learn to exploit.
RAIL_BASE_SR: dict[Rail, float] = {
    Rail.UPI_INTENT: 0.94,
    Rail.UPI_COLLECT: 0.76,
    Rail.UPI_AUTOPAY: 0.88,
    Rail.CARD: 0.87,
    Rail.CARD_RECURRING: 0.82,
    Rail.NETBANKING: 0.85,
    Rail.ENACH: 0.80,
    Rail.WALLET: 0.93,
}

# Decline-code mix on failure, by rail. These shapes are what the degradation
# detector's "characteristic tell" is measured against.
FAILURE_MIX: dict[Rail, dict[str, float]] = {
    Rail.UPI_AUTOPAY: {"INSUFFICIENT_FUNDS": 0.52, "MANDATE_PAUSED": 0.10,
                       "BANK_DOWN": 0.14, "MANDATE_REVOKED": 0.08,
                       "NPCI_UNAVAILABLE": 0.09, "AP12": 0.07},
    Rail.ENACH: {"INSUFFICIENT_FUNDS": 0.55, "AP01": 0.08, "AP12": 0.10,
                 "BANK_DOWN": 0.12, "MANDATE_REVOKED": 0.09, "AP08": 0.06},
    Rail.CARD_RECURRING: {"INSUFFICIENT_FUNDS": 0.34, "CARD_EXPIRED": 0.22,
                          "CARD_BLOCKED": 0.12, "AP39": 0.14,
                          "GATEWAY_ERROR": 0.10, "ACCOUNT_CLOSED": 0.08},
    Rail.CARD: {"INSUFFICIENT_FUNDS": 0.30, "AP39": 0.26, "CARD_EXPIRED": 0.12,
                "GATEWAY_ERROR": 0.16, "CARD_BLOCKED": 0.08, "AUTH_TIMEOUT": 0.08},
    Rail.UPI_INTENT: {"UPI_PIN_INCORRECT": 0.24, "INSUFFICIENT_FUNDS": 0.28,
                      "AUTH_TIMEOUT": 0.20, "BANK_DOWN": 0.16, "NPCI_UNAVAILABLE": 0.12},
    Rail.UPI_COLLECT: {"COLLECT_EXPIRED": 0.42, "INSUFFICIENT_FUNDS": 0.22,
                       "AUTH_TIMEOUT": 0.16, "BANK_DOWN": 0.12, "NPCI_UNAVAILABLE": 0.08},
    Rail.NETBANKING: {"INSUFFICIENT_FUNDS": 0.32, "BANK_DOWN": 0.28,
                      "AUTH_TIMEOUT": 0.22, "GATEWAY_ERROR": 0.18},
    Rail.WALLET: {"INSUFFICIENT_FUNDS": 0.58, "GATEWAY_ERROR": 0.24, "AUTH_TIMEOUT": 0.18},
}

# Which webhook a failed obligation surfaces as. Mirrors Razorpay's own event
# vocabulary so the sandbox's payloads are recognisable.
EVENT_FOR_KIND: dict[ObligationKind, str] = {
    ObligationKind.SUBSCRIPTION_CYCLE: "subscription.pending",
    ObligationKind.CART: "cart.abandoned",
    ObligationKind.INVOICE: "invoice.overdue",
    ObligationKind.ORDER: "payment.failed",
}


@dataclass(slots=True)
class GeneratedEvent:
    """One row of the replayable event log."""

    event_id: str
    event_type: str
    ts: datetime
    merchant_id: str
    customer_id: str
    obligation_id: str
    obligation_kind: str
    amount_paise: int
    rail: str
    issuer: str
    psp: str
    decline_code: str | None
    decline_text: str | None
    attempt_id: str
    due_at: datetime
    version: int


@dataclass(slots=True)
class Dataset:
    merchants: list[Merchant]
    events: list[GeneratedEvent]
    obligations: list[dict]
    attempts: list[dict]
    downtime: list[DowntimeWindow]
    degradations: list[DegradationEpisode]
    promises: list[Promise]


def _decline_text(code: str, issuer: str) -> str:
    """Free-text decline strings as issuers actually emit them.

    Deliberately messy and inconsistent, because this field is untrusted input
    that reaches the LLM classifier. It is also where an injection attempt would
    arrive in the real world, which the agent tests exercise.
    """
    templates = {
        "INSUFFICIENT_FUNDS": [f"{issuer}: insufficient balance in account",
                               "Not enough funds", "DEBIT FAILED - LOW BAL"],
        "BANK_DOWN": [f"{issuer} host unavailable", "Issuer down, try later",
                      "REMITTER BANK UNAVAILABLE"],
        "AP39": ["OTP validation failed", "Invalid OTP entered", "AP39 - OTP INVALID"],
        "MANDATE_REVOKED": ["Mandate cancelled by customer", "UMN revoked",
                            "e-mandate no longer active"],
        "CARD_EXPIRED": ["Card expired", "EXPIRED CARD", "Card validity ended"],
    }
    return random.choice(templates.get(code, [f"{code} at {issuer}"]))


def _sr_for(
    rail: Rail, ts: datetime, issuer: str, psp: str,
    downtime: list[DowntimeWindow], degradations: list[DegradationEpisode],
) -> tuple[float, str | None]:
    """Effective success rate at a moment, plus any code whose share is spiking."""
    sr = RAIL_BASE_SR[rail]
    forced_code: str | None = None

    # Funds-sensitive rails track the salary cycle.
    #
    # The swing is deliberately large (~0.70x mid-month against 1.0x just after
    # salary credit). An earlier version used a 15% swing, which left the
    # learned day-of-month curve almost flat (0.97-1.07) and inconsistent with
    # the outcome oracle, where retry timing quality spans 3x. Insufficient-
    # funds failures really are far more common mid-month, and the model has to
    # be able to see that in the data it is fitted on.
    if rail.is_mandate or rail in (Rail.NETBANKING, Rail.WALLET):
        sr *= 0.55 + 0.45 * min(1.0, balance_availability(ts.day) / 1.35)

    if is_weekend(ts.date()):
        sr *= 0.985

    for w in downtime:
        if w.issuer == issuer and w.covers(ts):
            sr *= 1.0 - w.severity
            forced_code = "BANK_DOWN"

    for e in degradations:
        hit = (e.dimension == "issuer" and e.value == issuer) or (
            e.dimension == "psp" and e.value == psp
        )
        if hit and e.covers(ts):
            sr = max(0.02, sr - e.sr_drop)
            forced_code = e.dominant_code

    return max(0.02, min(0.995, sr)), forced_code


def generate(
    seed: int = 20260822,
    days: int = 90,
    customers_per_merchant: int = 1500,
    start: datetime | None = None,
) -> Dataset:
    rng = random.Random(seed)
    random.seed(seed)
    start = start or datetime(2026, 5, 1, 0, 0, 0)

    world = build_world(rng, customers_per_merchant)
    downtime = generate_downtime_windows(seed, ISSUERS, start, days)
    degradations = generate_degradation_episodes(seed, ISSUERS, PSPS, start, days)

    events: list[GeneratedEvent] = []
    obligations: list[dict] = []
    attempts: list[dict] = []

    for merchant in world:
        spec: MerchantSpec = merchant.spec
        for day in range(days):
            d = start + timedelta(days=day)
            # B2B invoices are raised on business days only.
            if spec.segment == "b2b_services" and is_weekend(d.date()):
                continue
            n = int(spec.daily_obligations * rng.uniform(0.85, 1.15))
            for _ in range(n):
                cust = rng.choice(merchant.customers)
                kind = rng.choice(spec.kinds)
                rail = _weighted(rng, spec.rails)
                amount = rng.randint(*spec.amount_range)
                psp = rng.choice(PSPS)

                # Mandate debits fire in the morning batch; customer-initiated
                # payments follow a daytime distribution.
                hour = rng.randint(6, 10) if rail.is_mandate else rng.choices(
                    range(24),
                    weights=[1,1,1,1,1,2,4,6,8,9,10,10,9,8,9,10,11,12,13,12,10,7,4,2],
                )[0]
                ts = d.replace(hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59))

                sr, forced = _sr_for(rail, ts, cust.issuer, psp, downtime, degradations)
                succeeded = rng.random() < sr

                oid = obligation_id()
                # Invoices carry payment terms; everything else is due on the spot.
                due = ts + timedelta(days=rng.choice([15, 30, 45])) \
                    if kind is ObligationKind.INVOICE else ts

                obligations.append({
                    "id": oid, "merchant_id": merchant.id, "customer_id": cust.id,
                    "kind": kind.value, "amount_paise": amount, "currency": "INR",
                    "due_at": due, "state": "recovered" if succeeded else "open",
                    "version": 1, "source_ref": None,
                })

                aid = attempt_id()
                code = None
                if not succeeded:
                    code = forced if forced and rng.random() < 0.72 else _weighted(
                        rng, FAILURE_MIX[rail]
                    )
                attempts.append({
                    "id": aid, "obligation_id": oid, "rail": rail.value,
                    "issuer": cust.issuer, "psp": psp,
                    "status": "captured" if succeeded else "failed",
                    "decline_code": code,
                    "decline_text": _decline_text(code, cust.issuer) if code else None,
                    "amount_paise": amount, "attempted_at": ts,
                    "caused_by_action_id": None, "idempotency_key": None,
                })

                # Only failures become recovery opportunities. Successes still
                # enter the stream because the degradation detector needs a
                # denominator to compute a success rate against.
                events.append(GeneratedEvent(
                    event_id=event_id(),
                    event_type=EVENT_FOR_KIND[kind] if not succeeded else "payment.captured",
                    ts=ts, merchant_id=merchant.id, customer_id=cust.id,
                    obligation_id=oid, obligation_kind=kind.value,
                    amount_paise=amount, rail=rail.value, issuer=cust.issuer, psp=psp,
                    decline_code=code,
                    decline_text=_decline_text(code, cust.issuer) if code else None,
                    attempt_id=aid, due_at=due, version=1,
                ))

    events.sort(key=lambda e: e.ts)

    # Promises are derived from the finished obligation set rather than emitted
    # inside the loop, because a promise needs to know when its obligation
    # failed and the loop does not know that until the attempt is written.
    failed_at = {
        a["obligation_id"]: a["attempted_at"]
        for a in attempts if a["status"] == "failed"
    }
    promises = generate_promises(seed, obligations, failed_at, start + timedelta(days=days))

    return Dataset(world, events, obligations, attempts, downtime, degradations, promises)
