"""Incremental margin, and how confident we are in it.

Two numbers are computed for every arm, and reporting both is the point of this
module rather than a flourish:

**True incremental margin.** The oracle's counterfactual on every case: what the
arm produced minus what that same customer would have produced untouched.
Available only in simulation, and it is the ground truth.

**Holdout-estimated incremental margin.** The same quantity computed the way a
real deployment must — comparing treated customers against the ~10% who were
held out, with no counterfactual available for anyone else.

The gap between them is the interesting result. Every competitor in this market
reports gross recovery; the ones that claim lift do not publish how they
estimate it. Showing that a 10% holdout recovers the true causal number to
within a stated tolerance turns "we measure incrementality honestly" from a
claim into a demonstration — and it is the honest answer to the only question
that matters about a lift figure, which is *how do you know it is right?*

**The bootstrap resamples CUSTOMERS, not cases.** One person may hold an
abandoned cart, a failed subscription and an overdue invoice at once; their
outcomes are correlated because they are the same person having the same week.
Resampling cases would treat those as three independent observations and report
a confidence interval narrower than the truth — which, for a number whose whole
purpose is to be trusted, is the worst possible direction to be wrong in.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field

from yukti.eval.oracle_bridge import ArmOutcome

# Enough for a stable 95% interval without making `make eval` slow. The interval
# is reported to the nearest rupee, and 2,000 resamples settles well inside that.
BOOTSTRAP_ROUNDS = 2_000
CONFIDENCE = 0.95


@dataclass(frozen=True, slots=True)
class Interval:
    point: float
    low: float
    high: float

    @property
    def excludes_zero(self) -> bool:
        """Whether the effect is distinguishable from doing nothing at all."""
        return self.low > 0 or self.high < 0

    def __str__(self) -> str:
        return f"{self.point:+,.0f} [{self.low:+,.0f}, {self.high:+,.0f}]"


@dataclass(slots=True)
class ArmMetrics:
    """Everything reported for one arm."""

    arm: str
    cases: int = 0
    customers: int = 0
    actions_taken: int = 0
    contacts: int = 0
    recovered_cases: int = 0
    opt_outs: int = 0

    gross_recovered_paise: int = 0
    discount_spend_paise: int = 0
    channel_spend_paise: int = 0
    net_margin_paise: int = 0

    # Against the oracle's counterfactual for the same cases.
    true_incremental_margin_paise: int = 0
    # What a 10% holdout would have let you estimate, with its own CI — the
    # error against oracle truth is uninterpretable without one.
    holdout_incremental: Interval | None = None

    # Against the retry-only arm rather than against doing nothing: the margin
    # attributable to the CONTACT BUDGET alone. Every acting arm funds the same
    # free silent retries, and on this merchant that shared mass is ~30x the
    # contact volume — so it dominates the headline and hides the only decision
    # the arms actually differ on. This is the number the thesis is about.
    contact_incremental_margin_paise: int = 0
    contact_incremental_per_1k: Interval | None = None

    # Per 1,000 opportunities, which is how the headline is quoted.
    net_incremental_per_1k: Interval | None = None
    gross_recovered_per_1k: float = 0.0

    # How many recovered cases would have recovered anyway. The number that
    # makes a merchant trust the rest.
    would_have_recovered_anyway: int = 0

    by_archetype: dict[str, int] = field(default_factory=dict)

    @property
    def cost_paise(self) -> int:
        return self.discount_spend_paise + self.channel_spend_paise

    @property
    def cost_per_incremental_rupee(self) -> float:
        if self.true_incremental_margin_paise <= 0:
            return float("inf")
        return self.cost_paise / self.true_incremental_margin_paise

    @property
    def holdout_estimate_error(self) -> float:
        """Relative error of the holdout estimate against oracle truth."""
        truth = self.true_incremental_margin_paise
        if truth == 0 or self.holdout_incremental is None:
            return 0.0
        return (self.holdout_incremental.point - truth) / abs(truth)

    @property
    def holdout_brackets_truth(self) -> bool:
        """Does the holdout's interval contain the true value?

        This is the actual pass/fail for the measurement claim. A point estimate
        being off by 50% is fine if the interval covers the truth and is honestly
        wide; a tight interval that MISSES the truth is a broken estimator.
        """
        if self.holdout_incremental is None:
            return False
        return (self.holdout_incremental.low
                <= self.true_incremental_margin_paise
                <= self.holdout_incremental.high)


def summarise(
    arm: str, outcomes: list[ArmOutcome], baseline: dict[str, ArmOutcome],
    archetypes: dict[str, str], seed: int = 20260822,
) -> ArmMetrics:
    """Aggregate one arm's outcomes into the reported metrics.

    `baseline` is the holdout outcome for every case — the same customer under
    the same draw with nothing done. That pairing is what lets a few points of
    lift be measured at this sample size.
    """
    m = ArmMetrics(arm=arm, cases=len(outcomes))
    m.customers = len({o.customer_id for o in outcomes})

    for o in outcomes:
        base = baseline[o.case_id]

        if o.action_kind != "suppress":
            m.actions_taken += 1
        if o.contacted:
            m.contacts += 1
        if o.recovered:
            m.recovered_cases += 1
            # The line that earns the merchant's trust: this one recovered, and
            # it would have recovered without us.
            if base.recovered:
                m.would_have_recovered_anyway += 1
        if o.opted_out and not base.opted_out:
            # Only opt-outs we CAUSED. One that would have happened anyway is
            # not a cost of the policy being evaluated.
            m.opt_outs += 1

        m.gross_recovered_paise += o.recovered_paise
        m.discount_spend_paise += o.discount_paise
        m.channel_spend_paise += o.channel_cost_paise
        m.net_margin_paise += o.net_margin_paise
        m.true_incremental_margin_paise += o.net_margin_paise - base.net_margin_paise

        if o.recovered:
            archetype = archetypes.get(o.case_id, "unknown")
            m.by_archetype[archetype] = m.by_archetype.get(archetype, 0) + 1

    per_case = _incremental_by_customer(outcomes, baseline)
    m.net_incremental_per_1k = bootstrap_per_1k(per_case, seed=seed)
    m.gross_recovered_per_1k = (
        1000 * m.gross_recovered_paise / m.cases if m.cases else 0.0
    )
    return m


def against_reference(
    outcomes: list[ArmOutcome], reference: dict[str, ArmOutcome], seed: int = 20260822,
) -> tuple[int, Interval]:
    """Incremental margin against an arm other than the do-nothing holdout.

    Same paired logic as the headline — the oracle's draw is keyed on the case,
    so an arm and its reference face the same customer with the same luck — but
    the counterfactual is "what the cheap policy would have produced" rather than
    "what nobody would have produced". That isolates the marginal decision
    instead of re-measuring the mass both arms share.
    """
    total = sum(o.net_margin_paise - reference[o.case_id].net_margin_paise
                for o in outcomes)
    return total, bootstrap_per_1k(
        _incremental_by_customer(outcomes, reference), seed=seed)


def _incremental_by_customer(
    outcomes: list[ArmOutcome], baseline: dict[str, ArmOutcome]
) -> dict[str, list[int]]:
    """Per-customer incremental margins, grouped so the bootstrap can resample
    the customer as a unit."""
    grouped: dict[str, list[int]] = defaultdict(list)
    for o in outcomes:
        delta = o.net_margin_paise - baseline[o.case_id].net_margin_paise
        grouped[o.customer_id].append(delta)
    return grouped


def bootstrap_per_1k(
    by_customer: dict[str, list[int]], rounds: int = BOOTSTRAP_ROUNDS,
    confidence: float = CONFIDENCE, seed: int = 20260822,
) -> Interval:
    """Net incremental margin per 1,000 opportunities, with a percentile CI.

    Customers are the resampling unit. A customer contributing three cases
    contributes all three or none to a given resample, which preserves the
    correlation between their outcomes instead of pretending it away.
    """
    customers = list(by_customer)
    if not customers:
        return Interval(0.0, 0.0, 0.0)

    def per_1k(sample: list[str]) -> float:
        total = 0
        cases = 0
        for c in sample:
            deltas = by_customer[c]
            total += sum(deltas)
            cases += len(deltas)
        return 1000 * total / cases if cases else 0.0

    point = per_1k(customers)

    rng = random.Random(seed)
    n = len(customers)
    draws = sorted(
        per_1k([customers[rng.randrange(n)] for _ in range(n)])
        for _ in range(rounds)
    )

    tail = (1 - confidence) / 2
    low = draws[max(0, int(math.floor(tail * rounds)))]
    high = draws[min(rounds - 1, int(math.ceil((1 - tail) * rounds)) - 1)]
    return Interval(point=point, low=low, high=high)


def holdout_estimate(
    outcomes: list[ArmOutcome], baseline: dict[str, ArmOutcome],
    assigned_arm: dict[str, str], total_cases: int,
    rounds: int = BOOTSTRAP_ROUNDS, seed: int = 20260822,
) -> Interval:
    """Incremental margin as a real deployment would have to estimate it.

    Only the ~10% of customers deterministically assigned to the holdout are
    observed untreated. Their mean margin stands in for what the treated cases
    would have produced, and the difference scales to the population. No oracle
    is used — that is the whole point. This is the number the system could
    actually report to a merchant.

    **It is returned with a confidence interval, and that is not decoration.**
    Ten percent of a few thousand cases is a few hundred observations, so this
    estimate is genuinely noisy, and a point estimate alone cannot distinguish
    "the estimator is biased" from "the holdout was small". Reporting the
    interval is what lets a reader tell whether an error against the oracle
    truth is a real problem or the sampling noise you would expect — and a lift
    number quoted without one is exactly the sort of claim this project exists
    to be suspicious of.

    Both arms are resampled by CUSTOMER for the same reason as everywhere else:
    one person's several obligations are not independent observations.
    """
    treated = [o for o in outcomes if assigned_arm.get(o.case_id) != "holdout"]
    held = [o for o in outcomes if assigned_arm.get(o.case_id) == "holdout"]

    if not treated or not held:
        return Interval(0.0, 0.0, 0.0)

    # A holdout case is never acted on, so its observed margin IS its
    # counterfactual — which is exactly why the holdout has to be assigned
    # before anything is decided rather than carved out afterwards.
    treated_by_customer: dict[str, list[int]] = defaultdict(list)
    for o in treated:
        treated_by_customer[o.customer_id].append(o.net_margin_paise)

    held_by_customer: dict[str, list[int]] = defaultdict(list)
    for o in held:
        held_by_customer[o.customer_id].append(baseline[o.case_id].net_margin_paise)

    def estimate(t_keys: list[str], h_keys: list[str]) -> float:
        t_vals = [v for k in t_keys for v in treated_by_customer[k]]
        h_vals = [v for k in h_keys for v in held_by_customer[k]]
        if not t_vals or not h_vals:
            return 0.0
        return (sum(t_vals) / len(t_vals) - sum(h_vals) / len(h_vals)) * total_cases

    t_customers = list(treated_by_customer)
    h_customers = list(held_by_customer)
    point = estimate(t_customers, h_customers)

    rng = random.Random(seed)
    nt, nh = len(t_customers), len(h_customers)
    draws = sorted(
        estimate(
            [t_customers[rng.randrange(nt)] for _ in range(nt)],
            [h_customers[rng.randrange(nh)] for _ in range(nh)],
        )
        for _ in range(rounds)
    )

    tail = (1 - CONFIDENCE) / 2
    return Interval(
        point=point,
        low=draws[max(0, int(math.floor(tail * rounds)))],
        high=draws[min(rounds - 1, int(math.ceil((1 - tail) * rounds)) - 1)],
    )


def compare(a: ArmMetrics, b: ArmMetrics) -> str:
    """One line describing how two arms differ, for the report."""
    gross = a.gross_recovered_paise - b.gross_recovered_paise
    net = a.true_incremental_margin_paise - b.true_incremental_margin_paise
    return (
        f"{a.arm} vs {b.arm}: gross {gross / 100:+,.0f} rupees, "
        f"net incremental {net / 100:+,.0f} rupees"
    )
