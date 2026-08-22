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
    # Observable prior behaviour. A legitimate feature source, unlike archetype.
    history: CustomerHistory | None = None
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


# Archetype is the LATENT CAUSE of observable history, not a draw conditioned
# on it.
#
# This is the second correction to this code, and the reasoning matters.
# Version 1 drew archetype independently of everything, so uplift was
# unlearnable in principle. Version 2 drew archetype conditioned on a history
# that had been generated first — better, but it produced sure things and
# persuadables with nearly identical observable profiles (tenure 432 vs 345,
# prior payments 5.7 vs 3.4) despite control recovery rates of 0.707 vs 0.089.
# Measured ceiling from observables was AUC 0.62 against 0.861 using the
# archetype directly, and no hyperparameter setting moved it, because the
# information simply was not in the features.
#
# Generating history FROM the archetype fixes that and is the honest causal
# story: a customer's disposition is what produces their payment record, not
# the other way round. It also preserves the marginal mix exactly, with no
# rebalancing needed.
#
# The discriminator that actually separates sure things from persuadables is
# whether past payments followed a nudge. A real system knows this — it can see
# whether a payment landed within the attribution window of a dunning contact —
# so it is a legitimate feature and not a smuggled label.
ARCHETYPE_BEHAVIOUR: dict[UpliftArchetype, dict[str, tuple[float, float]]] = {
    # (mean, spread) for each observable trait
    UpliftArchetype.SURE_THING: {
        "tenure": (620, 200),
        "unprompted_payments": (7.0, 2.8),   # pays without being asked
        "prompted_payments": (1.4, 1.7),     # occasionally pays after a nudge
        "failures": (0.8, 1.0),
        "contacts": (1.5, 1.5),
        "days_since_payment": (20, 15),
        "optout_rate": 0.02,
    },
    UpliftArchetype.PERSUADABLE: {
        "tenure": (330, 180),
        "unprompted_payments": (1.4, 1.5),   # seldom pays unprompted
        "prompted_payments": (4.0, 2.4),     # but responds well to a nudge
        "failures": (2.5, 1.5),
        "contacts": (5.5, 2.5),
        "days_since_payment": (45, 25),
        "optout_rate": 0.03,
    },
    UpliftArchetype.SLEEPING_DOG: {
        "tenure": (480, 200),
        "unprompted_payments": (5.0, 2.5),   # pays fine when left alone
        "prompted_payments": (1.0, 1.4),     # nudges rarely convert
        "failures": (1.2, 1.2),
        "contacts": (4.5, 2.2),              # has been contacted a lot
        "days_since_payment": (30, 20),
        "optout_rate": 0.38,                 # and resents it
    },
    UpliftArchetype.LOST_CAUSE: {
        "tenure": (240, 160),
        "unprompted_payments": (0.5, 1.0),
        "prompted_payments": (0.6, 1.1),
        "failures": (4.5, 2.0),
        "contacts": (4.0, 2.5),
        "days_since_payment": (160, 90),
        "optout_rate": 0.08,
    },
}


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    """Observable prior behaviour. Every field here is a legitimate feature.

    Caused by the archetype but observed with noise, so the disposition is
    inferable without being readable. That gap is deliberate: a real recovery
    system never knows for certain who it is dealing with, and a dataset where
    it did would make the whole exercise trivial.
    """

    tenure_days: int
    ltv_band: str
    prior_payments: int
    prior_failures: int
    prior_contacts: int
    prior_contact_responses: int
    prior_optouts: int
    days_since_last_payment: int
    # The discriminator. Split out because "paid after we asked" and "paid on
    # their own" mean very different things about what an intervention is worth.
    prior_unprompted_payments: int
    prior_prompted_payments: int

    @property
    def profile(self) -> str:
        """Coarse label, for diagnostics only. Models read the raw fields."""
        if self.prior_optouts > 0:
            return "annoyed_withdrawn"
        if self.prior_failures >= 3 and self.prior_payments <= 1:
            return "dormant_failing"
        if self.prior_prompted_payments >= 2 and self.prior_prompted_payments > self.prior_unprompted_payments:
            return "responds_to_nudges"
        if self.prior_unprompted_payments >= 4 and self.prior_prompted_payments <= 1:
            return "pays_unprompted"
        return "unremarkable"


def _draw_count(rng: random.Random, mean: float, spread: float) -> int:
    """Non-negative integer around a mean, with generous spread.

    The spread is what stops the archetype being readable straight off a single
    feature. Too tight and the problem is trivial; too loose and it is
    unlearnable.

    Tuned against a purity test: an earlier setting left
    prior_prompted_payments >= 4 a PERFECT readout of "persuadable", which is
    leakage wearing a different name — the model would read a counter rather
    than infer a disposition, and the gate would flatter it. The distributions
    now overlap enough that no single value determines the archetype, so the
    signal has to come from the combination (prompted share, unprompted volume,
    opt-out history, recency) rather than one threshold.
    """
    return max(0, int(round(rng.gauss(mean, spread))))


def build_history(
    rng: random.Random, spec: MerchantSpec, archetype: UpliftArchetype
) -> CustomerHistory:
    """Generate an observable history caused by the archetype."""
    b = ARCHETYPE_BEHAVIOUR[archetype]

    tenure = max(1, _draw_count(rng, *b["tenure"]))
    unprompted = _draw_count(rng, *b["unprompted_payments"])
    prompted = _draw_count(rng, *b["prompted_payments"])
    failures = _draw_count(rng, *b["failures"])
    contacts = max(prompted, _draw_count(rng, *b["contacts"]))
    days_since = max(1, _draw_count(rng, *b["days_since_payment"]))

    payments = unprompted + prompted
    if payments == 0:
        # Never paid: recency must be consistent with that, not contradict it.
        days_since = max(days_since, 90)

    optouts = 1 if rng.random() < b["optout_rate"] else 0

    ltv = "high" if payments >= 7 else "mid" if payments >= 3 else "low"
    if rng.random() < 0.20:   # noise, so LTV is not a deterministic readout
        ltv = _weighted(rng, {"low": 0.5, "mid": 0.35, "high": 0.15})

    return CustomerHistory(
        tenure_days=tenure,
        ltv_band=ltv,
        prior_payments=payments,
        prior_failures=failures,
        prior_contacts=contacts,
        prior_contact_responses=prompted,
        prior_optouts=optouts,
        days_since_last_payment=days_since,
        prior_unprompted_payments=unprompted,
        prior_prompted_payments=prompted,
    )


def build_customers(
    rng: random.Random, merchant_db_id: str, spec: MerchantSpec, n: int
) -> list[Customer]:
    """Populate a merchant's customer base.

    Archetype first — so the marginal mix is exact by construction — then an
    observable history caused by it.
    """
    out: list[Customer] = []
    for _ in range(n):
        archetype = _weighted(rng, ARCHETYPE_MIX)

        # B2B buyers are institutional: they neither farm discounts nor sulk.
        if spec.segment == "b2b_services" and archetype is UpliftArchetype.SLEEPING_DOG:
            archetype = UpliftArchetype.PERSUADABLE

        history = build_history(rng, spec, archetype)

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
                ltv_band=history.ltv_band,
                tenure_days=history.tenure_days,
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
                history=history,
                opted_out_at=None,
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
