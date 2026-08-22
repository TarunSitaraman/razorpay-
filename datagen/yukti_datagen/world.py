"""Merchant and customer population for the synthetic world.

Six merchants spanning all four revenue-loss surfaces named in the track brief,
because the cross-surface arbitration gap only shows up when one customer can
owe a merchant money in more than one way at the same time.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from yukti.domain.enums import Channel, ObligationKind, Rail, UpliftArchetype
from yukti.domain.ids import customer_id, merchant_id

# Real Indian issuers and PSPs, used as opaque labels. Nothing here asserts
# anything about these institutions' actual reliability — they are names on
# synthetic buckets so the dataset reads like a real one.
ISSUERS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "PNB", "BOB", "YESBANK"]
PSPS = ["razorpay_psp_a", "razorpay_psp_b", "razorpay_psp_c"]

# Archetype mix. Roughly a third of failures resolve themselves, which is why
# reporting gross recovery flatters every tool in this market.
ARCHETYPE_MIX: dict[UpliftArchetype, float] = {
    UpliftArchetype.SURE_THING: 0.30,
    UpliftArchetype.PERSUADABLE: 0.25,
    UpliftArchetype.LOST_CAUSE: 0.30,
    UpliftArchetype.SLEEPING_DOG: 0.15,
}


@dataclass(frozen=True, slots=True)
class MerchantSpec:
    name: str
    segment: str
    kinds: tuple[ObligationKind, ...]
    rails: tuple[tuple[Rail, float], ...]     # rail, share
    amount_range: tuple[int, int]             # paise
    mdr_bps: int
    daily_obligations: int
    # Contact and discount budgets the merchant is willing to fund per day.
    contact_budget_per_day: int
    discount_budget_paise_per_day: int


MERCHANTS: tuple[MerchantSpec, ...] = (
    MerchantSpec(
        name="Chai Point Subscriptions", segment="d2c_subscription",
        kinds=(ObligationKind.SUBSCRIPTION_CYCLE, ObligationKind.CART),
        rails=((Rail.UPI_AUTOPAY, 0.55), (Rail.CARD_RECURRING, 0.30), (Rail.ENACH, 0.15)),
        amount_range=(29_900, 89_900), mdr_bps=200,
        daily_obligations=140, contact_budget_per_day=90,
        discount_budget_paise_per_day=1_50_000,
    ),
    MerchantSpec(
        name="Vidya Learning", segment="edtech",
        kinds=(ObligationKind.SUBSCRIPTION_CYCLE, ObligationKind.CART, ObligationKind.ORDER),
        rails=((Rail.UPI_INTENT, 0.35), (Rail.CARD, 0.25), (Rail.UPI_AUTOPAY, 0.25),
               (Rail.NETBANKING, 0.15)),
        amount_range=(4_99_000, 24_99_000), mdr_bps=180,
        daily_obligations=90, contact_budget_per_day=70,
        discount_budget_paise_per_day=8_00_000,
    ),
    MerchantSpec(
        name="Kirana Cloud SaaS", segment="saas",
        kinds=(ObligationKind.SUBSCRIPTION_CYCLE, ObligationKind.INVOICE),
        rails=((Rail.CARD_RECURRING, 0.45), (Rail.ENACH, 0.35), (Rail.UPI_AUTOPAY, 0.20)),
        amount_range=(1_49_900, 9_99_900), mdr_bps=220,
        daily_obligations=70, contact_budget_per_day=50,
        discount_budget_paise_per_day=3_00_000,
    ),
    MerchantSpec(
        name="Sahyog Finance EMI", segment="nbfc_lending",
        kinds=(ObligationKind.SUBSCRIPTION_CYCLE,),
        rails=((Rail.ENACH, 0.60), (Rail.UPI_AUTOPAY, 0.40)),
        amount_range=(2_50_000, 45_00_000), mdr_bps=90,
        daily_obligations=110, contact_budget_per_day=100,
        discount_budget_paise_per_day=0,   # lenders do not discount principal
    ),
    MerchantSpec(
        name="Bazaar Direct", segment="marketplace",
        kinds=(ObligationKind.CART, ObligationKind.ORDER),
        rails=((Rail.UPI_INTENT, 0.45), (Rail.UPI_COLLECT, 0.15), (Rail.CARD, 0.25),
               (Rail.WALLET, 0.15)),
        amount_range=(39_900, 7_99_900), mdr_bps=210,
        daily_obligations=200, contact_budget_per_day=150,
        discount_budget_paise_per_day=6_00_000,
    ),
    MerchantSpec(
        # The B2B receivables surface: the track page names both a "B2B
        # receivables chaser" and a "promise-to-pay tracker".
        name="Meridian Logistics B2B", segment="b2b_services",
        kinds=(ObligationKind.INVOICE,),
        rails=((Rail.NETBANKING, 0.45), (Rail.ENACH, 0.35), (Rail.UPI_INTENT, 0.20)),
        amount_range=(25_00_000, 4_00_00_000), mdr_bps=60,
        daily_obligations=25, contact_budget_per_day=25,
        discount_budget_paise_per_day=0,   # B2B negotiates terms, not discounts
    ),
)


@dataclass(slots=True)
class Customer:
    id: str
    merchant_id: str
    archetype: UpliftArchetype
    ltv_band: str
    tenure_days: int
    preferred_channel: Channel
    consent: dict[str, bool]
    issuer: str
    # A small cohort learns that abandoning produces a discount. Detecting them
    # is a policy-engine concern; generating them is this file's concern.
    discount_farmer: bool = False
    opted_out_at: datetime | None = None
    # Rolling contact log, used to compute trailing-7d fatigue during generation.
    contact_log: list[datetime] = field(default_factory=list)

    def contacts_in_window(self, ts: datetime, days: int = 7) -> int:
        cutoff = ts - timedelta(days=days)
        return sum(1 for t in self.contact_log if t >= cutoff)


def _weighted(rng: random.Random, options: dict | tuple):
    items = list(options.items()) if isinstance(options, dict) else list(options)
    total = sum(w for _, w in items)
    r = rng.random() * total
    upto = 0.0
    for value, weight in items:
        upto += weight
        if r <= upto:
            return value
    return items[-1][0]


def build_customers(
    rng: random.Random, merchant_db_id: str, spec: MerchantSpec, n: int
) -> list[Customer]:
    """Populate a merchant's customer base with ground-truth archetypes."""
    out: list[Customer] = []
    for _ in range(n):
        archetype = _weighted(rng, ARCHETYPE_MIX)
        # B2B buyers are institutional: they neither farm discounts nor sulk.
        if spec.segment == "b2b_services" and archetype is UpliftArchetype.SLEEPING_DOG:
            archetype = UpliftArchetype.PERSUADABLE

        channel = _weighted(
            rng,
            {Channel.WHATSAPP: 0.5, Channel.SMS: 0.25, Channel.EMAIL: 0.2, Channel.VOICE: 0.05}
            if spec.segment != "b2b_services"
            else {Channel.EMAIL: 0.7, Channel.WHATSAPP: 0.2, Channel.VOICE: 0.1},
        )
        out.append(
            Customer(
                id=customer_id(),
                merchant_id=merchant_db_id,
                archetype=archetype,
                ltv_band=_weighted(rng, {"low": 0.5, "mid": 0.35, "high": 0.15}),
                tenure_days=rng.randint(1, 900),
                preferred_channel=channel,
                consent={
                    # Consent is per channel under DPDP. Not everyone grants it,
                    # and the policy engine must honour the gaps.
                    "whatsapp": rng.random() < 0.82,
                    "sms": rng.random() < 0.93,
                    "email": rng.random() < 0.97,
                    "voice": rng.random() < 0.55,
                },
                issuer=rng.choice(ISSUERS),
                discount_farmer=rng.random() < 0.03,
            )
        )
    return out


@dataclass(slots=True)
class Merchant:
    id: str
    spec: MerchantSpec
    customers: list[Customer]


def build_world(rng: random.Random, customers_per_merchant: int) -> list[Merchant]:
    return [
        Merchant(
            id=(mid := merchant_id()),
            spec=spec,
            customers=build_customers(rng, mid, spec, customers_per_merchant),
        )
        for spec in MERCHANTS
    ]
