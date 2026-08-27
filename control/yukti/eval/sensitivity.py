"""Sweeping the assumptions the headline result rests on.

**Why this module exists.** The headline evaluation trains a learner on data
produced by `datagen/yukti_datagen/response.py` and then grades its decisions
with that same oracle. The defences usually offered — the archetype is never a
feature, treatment was randomised — answer a *different* objection. They
establish that the learner did not cheat: that it recovered the generator's
structure from observables rather than reading the label. They say nothing about
whether that structure exists in real Indian payment data.

So "uplift beats propensity" is, in the headline run, a consequence of two
numbers chosen by the author: `max_uplift[PERSUADABLE] = 0.46` against
`max_uplift[SURE_THING] = 0.04`. Those are *inputs* to the experiment. A single
point estimate drawn from a world its author wrote is not evidence.

What this module produces instead is a **frontier**: vary each load-bearing
assumption across a plausible range and report the region in which the thesis
survives. That is a claim which can be checked and can fail, and it fails
honestly — `assumption_frontier()` reports the crossover point at which Niyama
stops beating a propensity ranker, and there is one.

**Faithfulness.** Every component here is the production component:

    the oracle          `yukti_datagen.response.evaluate`
    the feature frame   `intelligence.features.frame_from_rows`  (leak guard included)
    the learner         `intelligence.uplift.ActionConditionalUplift`
    the margin function `allocator.lagrangian.expected_margin`
    the allocator       `allocator.lagrangian.allocate`
    the confidence interval `eval.estimator.bootstrap_per_1k`

Nothing is reimplemented, because a sweep that reimplemented the thing it is
testing would be measuring its own reimplementation.

What is *not* here is the database, Kafka, the policy engine and the stopping
rules. Those are identical across arms by construction (`eval/arms.py`), so they
cannot explain a difference between arms — which is the only quantity this
module reports. The upside of leaving them out is that this runs from a clean
checkout with no services, so the frontier is reproducible by anyone in about a
minute.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd
from yukti_datagen.history import sample_intervention
from yukti_datagen.response import (
    DEFAULT_PARAMS,
    CaseContext,
    Intervention,
    OracleParams,
    evaluate,
)
from yukti_datagen.world import ARCHETYPE_MIX, build_history

from yukti.allocator.lagrangian import Budgets, Candidate, allocate, expected_margin
from yukti.dispatch.tools import CHANNEL_COST_PAISE
from yukti.domain.decline import lookup
from yukti.domain.enums import ActionKind, Channel, ObligationKind, Rail, UpliftArchetype
from yukti.eval.estimator import Interval, bootstrap_per_1k
from yukti.intelligence.features import frame_from_rows
from yukti.intelligence.uplift import ActionConditionalUplift

# One day, fixed. The sweep varies world assumptions, not the calendar.
AS_OF = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)

# Share of the population assigned to the exploration (treated) arm when the
# training history is generated. Matches the production exploration rate.
EXPLORE_TREATED_SHARE = 0.75

ISSUERS = ("HDFC", "ICICI", "SBI", "AXIS", "KOTAK")
PSPS = ("razorpay_psp_a", "razorpay_psp_b", "razorpay_psp_c")

# Decline mix, weighted toward the buckets that dominate Indian recurring
# payments. Recoverability varies sharply across these, which is what stops the
# allocation problem from being "rank by amount".
DECLINE_MIX: dict[str, float] = {
    "INSUFFICIENT_FUNDS": 0.30,
    "EXCEEDS_LIMIT": 0.06,
    "BANK_DOWN": 0.08,
    "PSP_TIMEOUT": 0.05,
    "AP39": 0.09,
    "UPI_PIN_INCORRECT": 0.07,
    "AUTH_TIMEOUT": 0.06,
    "CARD_EXPIRED": 0.08,
    "MANDATE_PAUSED": 0.05,
    "AP12": 0.04,
    "MANDATE_REVOKED": 0.05,
    "ACCOUNT_CLOSED": 0.04,
    "CARD_BLOCKED": 0.03,
}

RAIL_MIX: dict[Rail, float] = {
    Rail.UPI_AUTOPAY: 0.34,
    Rail.CARD_RECURRING: 0.26,
    Rail.ENACH: 0.16,
    Rail.UPI_INTENT: 0.14,
    Rail.CARD: 0.10,
}

CHANNEL_MIX: dict[Channel, float] = {
    Channel.WHATSAPP: 0.44,
    Channel.SMS: 0.31,
    Channel.EMAIL: 0.20,
    Channel.VOICE: 0.05,
}

MDR_BPS = 200
AMOUNT_RANGE = (29_900, 12_00_000)


def _weighted(rng: random.Random, options: dict):
    total = sum(options.values())
    r = rng.random() * total
    acc = 0.0
    for k, w in options.items():
        acc += w
        if r <= acc:
            return k
    return next(iter(options))


# ---------------------------------------------------------------------------
# The world under test
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class World:
    """A set of assumptions about how customers respond. The sweep axis.

    `params` holds the response model's constants; `mix` holds the population
    composition. Both are things the headline evaluation fixes silently, and
    both change the answer.
    """

    params: OracleParams = DEFAULT_PARAMS
    mix: dict[UpliftArchetype, float] = field(default_factory=lambda: dict(ARCHETYPE_MIX))
    label: str = "default"


@dataclass(frozen=True, slots=True)
class Case:
    """One open obligation, plus the observable row a model is allowed to see."""

    ctx: CaseContext
    customer_id: str
    observable: dict
    mdr_bps: int


def _observable_row(
    *,
    case_id: str,
    ctx: CaseContext,
    history,
    rail: Rail,
    consent: dict,
    first_obligation: bool,
    rng: random.Random,
) -> dict:
    """The row a model is allowed to see for one case.

    Column names match `intelligence.features.TRAINING_SQL` exactly, because
    `frame_from_rows` — the production feature contract, leak guard included —
    is what consumes this. Diverging here would mean the sweep trains on a
    different feature space than the deployed model, which is the one difference
    that would invalidate the whole comparison.

    `archetype` is carried deliberately and is dropped by `frame_from_rows`; it
    is the scoring label, and `FORBIDDEN` exists to guarantee it never survives
    into `Frame.X`. Passing it through the real guard rather than withholding it
    is what makes `tests/unit/test_sensitivity.py::TestFeatureLeakage` a test of
    something.
    """
    return {
        "case_id": case_id,
        "arm": "explore",
        "obligation_kind": (
            ObligationKind.SUBSCRIPTION_CYCLE.value if first_obligation
            else ObligationKind.INVOICE.value
        ),
        "amount_paise": ctx.amount_paise,
        "merchant_segment": "d2c_subscription",
        "mdr_bps": MDR_BPS,
        "rail": rail.value,
        "issuer": rng.choice(ISSUERS),
        "psp": rng.choice(PSPS),
        "decline_code": ctx.decline_code,
        "ltv_band": history.ltv_band,
        "tenure_days": history.tenure_days,
        "preferred_channel": ctx.preferred_channel.value,
        "prior_payments": history.prior_payments,
        "prior_failures": history.prior_failures,
        "prior_contacts": history.prior_contacts,
        "prior_contact_responses": history.prior_contact_responses,
        "prior_optouts": history.prior_optouts,
        "days_since_last_payment": history.days_since_last_payment,
        "prior_unprompted_payments": history.prior_unprompted_payments,
        "prior_prompted_payments": history.prior_prompted_payments,
        "consent": consent,
        "archetype": ctx.archetype.value,
    }


def build_population(world: World, n: int, seed: int) -> list[Case]:
    """Draw `n` cases from `world`.

    Customers may hold more than one open obligation — that is what makes the
    per-customer contact cap bind, and it is the constraint a per-agent budget
    structurally cannot see. Roughly a fifth of customers here hold two or three.
    """
    rng = random.Random(seed)
    cases: list[Case] = []
    i = 0
    while len(cases) < n:
        archetype = _weighted(rng, world.mix)
        customer_id = f"cust_{i:06d}"
        i += 1
        history = build_history(rng, _SPEC_STUB, archetype)
        preferred = _weighted(rng, CHANNEL_MIX)
        consent = {
            "whatsapp": rng.random() < 0.88,
            "sms": rng.random() < 0.95,
            "email": rng.random() < 0.90,
            "voice": rng.random() < 0.55,
        }

        n_obligations = 1 if rng.random() < 0.80 else rng.choice([2, 2, 3])
        for j in range(n_obligations):
            if len(cases) >= n:
                break
            decline = _weighted(rng, DECLINE_MIX)
            rail = _weighted(rng, RAIL_MIX)
            amount = rng.randint(*AMOUNT_RANGE)
            case_id = f"case_{len(cases):06d}"
            # Trailing-week contact load, from every agent. Drawn from the
            # customer's observable contact history so that fatigue correlates
            # with disposition the way it does in the generated world.
            prior_7d = min(4, max(0, int(round(rng.gauss(history.prior_contacts / 4.0, 1.0)))))
            ctx = CaseContext(
                case_id=case_id,
                archetype=archetype,
                amount_paise=amount,
                decline_code=decline,
                rail_is_mandate=rail.is_mandate,
                preferred_channel=preferred,
                prior_contacts_7d=prior_7d,
                open_promise=rng.random() < 0.07,
                in_downtime=rng.random() < 0.06,
            )
            observable = _observable_row(
                case_id=case_id, ctx=ctx, history=history, rail=rail,
                consent=consent, first_obligation=(j == 0), rng=rng,
            )
            cases.append(
                Case(ctx=ctx, customer_id=customer_id, observable=observable,
                     mdr_bps=MDR_BPS)
            )
    return cases


class _SpecStub:
    """`build_history` reads only the segment. Reused rather than reconstructed
    so the observable histories are drawn by the production code path."""
    segment = "d2c_subscription"


_SPEC_STUB = _SpecStub()


# ---------------------------------------------------------------------------
# Exploration: an RCT inside the world
# ---------------------------------------------------------------------------

def exploration_frame(world: World, cases: list[Case], seed: int):
    """Run a randomised exploration period and return a training frame.

    The action is drawn by `sample_intervention`, which never looks at the
    customer. That independence is the identification strategy: it makes
    E[Y | X, A=a] estimable for every action, which is what
    `ActionConditionalUplift` needs and what an observational log cannot give.
    """
    rng = random.Random(seed)
    rows = []
    for case in cases:
        treated = rng.random() < EXPLORE_TREATED_SHARE
        if treated:
            iv = sample_intervention(rng, AS_OF)
        else:
            iv = Intervention(kind=ActionKind.SUPPRESS, channel=Channel.NONE, at=AS_OF)
        outcome = evaluate(case.ctx, iv, seed, params=world.params)

        acted = iv.kind is not ActionKind.SUPPRESS
        discount_paise = int(round(case.ctx.amount_paise * iv.discount_pct / 100.0))
        row = dict(case.observable)
        row.update(
            action_kind=iv.kind.value if acted else None,
            action_channel=iv.channel.value if acted else None,
            scheduled_for=iv.at,
            cost_paise=CHANNEL_COST_PAISE.get(iv.channel, 0) if acted else 0,
            discount_paise=discount_paise if acted else 0,
            discount_pct=iv.discount_pct if acted else 0.0,
            treated=acted,
            recovered=outcome.recovered,
        )
        rows.append(row)
    # `frame_from_rows` is the production contract, leak guard included: if a
    # forbidden column ever reached this frame it would raise here, in the sweep,
    # exactly as it would in training.
    return frame_from_rows(pd.DataFrame(rows))


def train(world: World, seed: int, n: int) -> ActionConditionalUplift:
    """Fit the real action-conditional model on this world's exploration data."""
    cases = build_population(world, n, seed=seed)
    frame = exploration_frame(world, cases, seed=seed)
    return ActionConditionalUplift(seed=seed).fit(frame)


# ---------------------------------------------------------------------------
# Planning: the action menu, scored, allocated, graded
# ---------------------------------------------------------------------------

DISCOUNT_TIERS = (5.0, 10.0)


def _menu(case: Case) -> list[Intervention]:
    """The actions the planner may propose for one case.

    Mirrors `candidates.generate`: a silent retry or scheduled debit where the
    decline permits one, a message on each consented channel, a discount at two
    tiers, and a voice call. Consent is honoured because the policy engine would
    reject the alternative before dispatch.
    """
    spec = lookup(case.ctx.decline_code)
    consent = case.observable["consent"]
    out: list[Intervention] = []

    if spec.retryable_silently and spec.max_attempts > 0:
        out.append(Intervention(ActionKind.SILENT_RETRY, Channel.NONE, AS_OF))
        out.append(Intervention(ActionKind.SCHEDULE_DEBIT, Channel.NONE, AS_OF))

    if spec.customer_actionable:
        for ch in (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP):
            if consent.get(ch.value):
                out.append(Intervention(ActionKind.MESSAGE, ch, AS_OF))
        preferred = case.ctx.preferred_channel
        if consent.get(preferred.value) and preferred is not Channel.VOICE:
            for tier in DISCOUNT_TIERS:
                out.append(
                    Intervention(ActionKind.DISCOUNT_OFFER, preferred, AS_OF, tier)
                )
        if consent.get("voice"):
            out.append(Intervention(ActionKind.VOICE_CALL, Channel.VOICE, AS_OF))
    return out


def _score_frame(cases: list[Case], menus: list[tuple[int, Intervention]]) -> pd.DataFrame:
    rows = []
    for idx, iv in menus:
        case = cases[idx]
        row = dict(case.observable)
        row.update(
            action_kind=iv.kind.value,
            action_channel=iv.channel.value,
            scheduled_for=iv.at,
            cost_paise=CHANNEL_COST_PAISE.get(iv.channel, 0),
            discount_paise=int(round(case.ctx.amount_paise * iv.discount_pct / 100.0)),
            discount_pct=iv.discount_pct,
            treated=True,
            recovered=False,          # placeholder; never read at scoring time
        )
        rows.append(row)
    return pd.DataFrame(rows)


# The arms. Each is a function from model scores to the number the allocator
# maximises — which is the ONLY thing that differs between them, exactly as in
# `eval/arms.py`.
ARM_KEYS = ("B4", "B1", "B3", "Y")
# The arm every other acting arm shares its free silent retries with. Measuring
# against THIS is what isolates the contact-allocation decision.
REFERENCE_ARM = "B4"
ARM_LABELS = {
    "B0": "holdout (no action)",
    "B4": "retry-only",
    "B1": "fixed cadence",
    "B3": "propensity only",
    "Y": "Niyama (uplift)",
}


def _arm_score(arm: str, uplift: float, propensity: float, iv: Intervention) -> float:
    if arm == "Y":
        return uplift
    if arm == "B3":
        return propensity
    if arm == "B1":
        return 0.05
    if arm == "B4":
        # Contacts are worth nothing, so none can clear its channel cost; the
        # costless silent retries are funded by the allocator's costless rule.
        return 0.0 if iv.kind.contacts_customer else uplift
    raise ValueError(arm)


@dataclass(slots=True)
class ArmResult:
    arm: str
    contacts: int
    recovered_cases: int
    opt_outs: int
    # Against the holdout: everything this arm did, free retries included.
    incremental_paise: int
    # Against retry-only: what the CONTACT decision alone was worth. This is the
    # number the frontier keys on, and the reason is measured, not stylistic --
    # see `run_arms`.
    contact_attributable_paise: int
    per_1k: Interval
    # Who the contact budget was actually spent on, by latent archetype. This is
    # ground truth and is legitimate HERE for exactly the reason
    # `eval/oracle_bridge.py` may read it: archetype is the scoring label, and
    # nothing here feeds back into a model.
    #
    # It is reported because it isolates the objective in a way money cannot.
    # Margin confounds "did it target the right people" with "were the amounts
    # large" — and a propensity ranker that contacts only sure things can still
    # look respectable if those happen to be big. The contact mix cannot be
    # confounded that way: it is the mechanism, stated directly.
    contacted_by_archetype: dict[str, int] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return ARM_LABELS[self.arm]


def _net_margin(case: Case, iv: Intervention | None, world: World, seed: int) -> int:
    """Realised net margin of one case under one action, in paise."""
    if iv is None:
        iv = Intervention(ActionKind.SUPPRESS, Channel.NONE, AS_OF)
    outcome = evaluate(case.ctx, iv, seed, params=world.params)
    gross = outcome.recovered_paise * (1 - case.mdr_bps / 10_000)
    cost = CHANNEL_COST_PAISE.get(iv.channel, 0) if iv.kind is not ActionKind.SUPPRESS else 0
    return int(round(gross - cost))


def _discount_paise(case: Case, iv: Intervention) -> int:
    return int(round(case.ctx.amount_paise * iv.discount_pct / 100.0))


def _candidates_for(
    arm: str, cases: list[Case], menus: list[tuple[int, Intervention]], scores
) -> list[Candidate]:
    """Price every proposed action under one arm's objective.

    The arms differ here and nowhere else: same menu, same margin function, same
    allocator, same grader. Only `_arm_score` changes.
    """
    out: list[Candidate] = []
    for row, (idx, iv) in enumerate(menus):
        case = cases[idx]
        score = _arm_score(arm, float(scores.uplift[row]),
                           float(scores.p_treated[row]), iv)
        discount = _discount_paise(case, iv)
        channel_cost = CHANNEL_COST_PAISE.get(iv.channel, 0)
        out.append(
            Candidate(
                case_id=case.ctx.case_id,
                customer_id=case.customer_id,
                action_kind=iv.kind.value,
                channel=iv.channel.value,
                margin_paise=expected_margin(
                    score, case.ctx.amount_paise, case.mdr_bps,
                    discount, channel_cost,
                ),
                contacts=1 if iv.kind.contacts_customer else 0,
                discount_paise=discount,
                channel_cost_paise=channel_cost,
            )
        )
    return out


def _funded_interventions(
    cases: list[Case], menus: list[tuple[int, Intervention]],
    chosen: list[Candidate],
) -> dict[str, Intervention]:
    """Map the allocator's chosen candidates back to the actions they describe.

    Matched on (kind, channel, discount) rather than by index because the
    allocator returns candidates in its own order. Within one case the menu never
    produces two entries agreeing on all three -- the two discount tiers differ
    in paise for every amount this generator emits -- so the match is unique.
    """
    by_case = {c.case_id: c for c in chosen}
    funded: dict[str, Intervention] = {}
    for idx, iv in menus:
        case = cases[idx]
        cid = case.ctx.case_id
        if cid in funded:
            continue
        picked = by_case.get(cid)
        if picked is None:
            continue
        if (picked.action_kind == iv.kind.value
                and picked.channel == iv.channel.value
                and picked.discount_paise == _discount_paise(case, iv)):
            funded[cid] = iv
    return funded


@dataclass(slots=True)
class _Graded:
    """One arm's realised outcome on every case."""

    realised: dict[str, int]
    contacts: int
    recovered: int
    opt_outs: int
    mix: dict[str, int]


def _grade(
    world: World, cases: list[Case], funded: dict[str, Intervention], seed: int
) -> _Graded:
    """Ask the oracle what each funded decision actually produced."""
    realised: dict[str, int] = {}
    mix: dict[str, int] = {}
    contacts = recovered = opt_outs = 0
    for case in cases:
        iv = funded.get(case.ctx.case_id)
        realised[case.ctx.case_id] = _net_margin(case, iv, world, seed)
        if iv is None:
            continue
        outcome = evaluate(case.ctx, iv, seed, params=world.params)
        recovered += int(outcome.recovered)
        opt_outs += int(outcome.opted_out)
        if iv.kind.contacts_customer:
            contacts += 1
            key = case.ctx.archetype.value
            mix[key] = mix.get(key, 0) + 1
    return _Graded(realised, contacts, recovered, opt_outs, mix)


def run_arms(
    world: World,
    model: ActionConditionalUplift,
    cases: list[Case],
    contact_budget: int,
    discount_budget_paise: int,
    per_customer_contacts: int,
    seed: int,
) -> dict[str, ArmResult]:
    """Score, allocate and grade every arm on the same cases.

    **Two baselines, and the second is the one that matters.**

    Against the holdout (no action at all) an arm's margin is dominated by the
    free silent retries, which the costless-action rule makes every acting arm
    fund identically. That shared mass is an order of magnitude larger than the
    contact budget, so measured against the holdout every arm looks the same and
    the frontier comes out flat -- a reassuring, meaningless picture. The first
    version of this module made exactly that mistake and reported a five-paise
    spread across a 7x change in the assumption the whole thesis rests on.

    Measured against **retry-only** the shared mass cancels exactly, and what
    remains is the only thing the arms disagree about: *who gets contacted.*
    That is `contact_attributable_paise`, and it is what `margin_over_rival` and
    the crossover are computed from.

    Both are reported, because the holdout number is what a merchant is billed
    against and the contact number is what isolates the decision.
    """
    menus: list[tuple[int, Intervention]] = [
        (i, iv) for i, case in enumerate(cases) for iv in _menu(case)
    ]
    if not menus:
        raise ValueError("no candidate actions were proposed")

    scores = model.predict(frame_from_rows(_score_frame(cases, menus)))
    baseline = {c.ctx.case_id: _net_margin(c, None, world, seed) for c in cases}
    budgets = Budgets(
        contacts=contact_budget,
        discount_paise=discount_budget_paise,
        per_customer_contacts=per_customer_contacts,
    )

    graded: dict[str, _Graded] = {}
    for arm in ARM_KEYS:
        candidates = _candidates_for(arm, cases, menus, scores)
        chosen = allocate(candidates, budgets).chosen
        funded = _funded_interventions(cases, menus, chosen)
        graded[arm] = _grade(world, cases, funded, seed)

    # The reference arm is graded like any other arm above rather than
    # special-cased, which is what keeps the subtraction below exact.
    reference = graded[REFERENCE_ARM].realised

    results: dict[str, ArmResult] = {}
    for arm in ARM_KEYS:
        g = graded[arm]
        by_customer: dict[str, list[int]] = {}
        total = attributable = 0
        for case in cases:
            cid = case.ctx.case_id
            total += g.realised[cid] - baseline[cid]
            delta = g.realised[cid] - reference[cid]
            attributable += delta
            by_customer.setdefault(case.customer_id, []).append(delta)

        results[arm] = ArmResult(
            arm=arm,
            contacts=g.contacts,
            recovered_cases=g.recovered,
            opt_outs=g.opt_outs,
            incremental_paise=total,
            contact_attributable_paise=attributable,
            per_1k=bootstrap_per_1k(by_customer, seed=seed),
            contacted_by_archetype=g.mix,
        )
    return results


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SweepPoint:
    axis: str
    value: float
    arms: dict[str, ArmResult]

    @property
    def winner(self) -> str:
        return max(self.arms, key=lambda k: self.arms[k].contact_attributable_paise)

    def margin_over_rival(self) -> int:
        """Niyama's contact-attributable margin minus the best rival's.

        Contact-attributable rather than total, because the free-retry mass is
        common to every acting arm and swamps the difference otherwise. Negative
        means the thesis fails at this point, which is a result this module is
        willing to print.
        """
        rival = max(
            (k for k in self.arms if k != "Y"),
            key=lambda k: self.arms[k].contact_attributable_paise,
        )
        return (self.arms["Y"].contact_attributable_paise
                - self.arms[rival].contact_attributable_paise)


AXES: dict[str, str] = {
    "persuadable_uplift": (
        "Headroom for the only profitable archetype. The single number the "
        "whole thesis rests on; the headline run assumes 0.46."
    ),
    "sleeping_dog_share": (
        "Population share of customers that contact actively harms. If they do "
        "not exist, avoiding them is worth nothing."
    ),
    "sure_thing_uplift": (
        "Headroom for customers who pay anyway. As this rises, propensity and "
        "uplift converge and the distinction stops paying."
    ),
    "silent_retry_irritation": (
        "Per-attempt opt-out risk of a retry the merchant cannot see. The "
        "allocator funds costless actions unconditionally on the assumption "
        "this is zero."
    ),
    "fatigue_decay": (
        "Response decay per prior contact. At 1.0 there is no cross-agent "
        "fatigue and per-customer arbitration earns nothing."
    ),
}


def world_at(axis: str, value: float) -> World:
    """The world with one assumption moved to `value`, all else default."""
    if axis == "persuadable_uplift":
        return World(
            params=DEFAULT_PARAMS.with_max_uplift(UpliftArchetype.PERSUADABLE, value),
            label=f"{axis}={value:g}",
        )
    if axis == "sure_thing_uplift":
        return World(
            params=DEFAULT_PARAMS.with_max_uplift(UpliftArchetype.SURE_THING, value),
            label=f"{axis}={value:g}",
        )
    if axis == "silent_retry_irritation":
        return World(
            params=DEFAULT_PARAMS.evolve(silent_retry_irritation=value),
            label=f"{axis}={value:g}",
        )
    if axis == "fatigue_decay":
        return World(
            params=DEFAULT_PARAMS.evolve(fatigue_decay=value),
            label=f"{axis}={value:g}",
        )
    if axis == "sleeping_dog_share":
        # Take the share out of (or into) sure things, so the persuadable
        # population — and therefore the size of the prize — is held fixed and
        # the axis measures only the cost of the harm-avoidance the system does.
        base = dict(ARCHETYPE_MIX)
        freed = base[UpliftArchetype.SLEEPING_DOG] - value
        mix = {
            **base,
            UpliftArchetype.SLEEPING_DOG: value,
            UpliftArchetype.SURE_THING: base[UpliftArchetype.SURE_THING] + freed,
        }
        return World(mix=mix, label=f"{axis}={value:g}")
    raise ValueError(f"unknown axis {axis!r}")


def sweep(
    axis: str,
    values: tuple[float, ...],
    n_train: int = 6_000,
    n_plan: int = 3_500,
    contact_budget: int = 90,
    discount_budget_paise: int = 1_50_000,
    per_customer_contacts: int = 1,
    seed: int = 20260822,
) -> list[SweepPoint]:
    """Refit and re-evaluate at every point on one axis.

    The model is **retrained at every grid point**, which is the expensive part
    and the whole point: a model fitted once in the default world and then
    scored in a different one would be measuring transfer, not the thesis. The
    question is whether uplift arbitration pays off in a world *given that you
    fitted it in that world* — which is the position a real deployment is in.
    """
    points: list[SweepPoint] = []
    for value in values:
        world = world_at(axis, value)
        model = train(world, seed=seed, n=n_train)
        plan_cases = build_population(world, n_plan, seed=seed + 977)
        arms = run_arms(
            world, model, plan_cases, contact_budget, discount_budget_paise,
            per_customer_contacts, seed=seed,
        )
        points.append(SweepPoint(axis=axis, value=value, arms=arms))
    return points


def crossover(points: list[SweepPoint]) -> float | None:
    """The axis value at which Niyama stops winning, by linear interpolation.

    Returns None if the sign never changes across the swept range. Reporting
    this is the difference between a sensitivity analysis and a victory lap.
    """
    for a, b in zip(points, points[1:], strict=False):
        ma, mb = a.margin_over_rival(), b.margin_over_rival()
        if (ma > 0) != (mb > 0):
            if ma == mb:
                return b.value
            t = ma / (ma - mb)
            return a.value + t * (b.value - a.value)
    return None


def crossover_direction(points: list[SweepPoint]) -> str:
    """Which side of the crossover the thesis stops surviving on.

    Accounts for grid ORIENTATION, not just which end wins. `persuadable_uplift`
    descends (0.46 -> 0.03) and wins at the start, so it fails below.
    `sure_thing_uplift` ascends (0.04 -> 0.34) and also wins at the start, so it
    fails ABOVE — once customers who would have paid anyway also respond to
    contact, propensity and uplift rank alike and the distinction stops paying.

    Keying only on "does the first point win" gets that second axis backwards,
    in the direction that reads as a stronger result than it is.
    """
    if len(points) < 2:
        return "below"
    first_wins = points[0].margin_over_rival() > 0
    failing = points[-1] if first_wins else points[0]
    surviving = points[0] if first_wins else points[-1]
    return "below" if failing.value < surviving.value else "above"
