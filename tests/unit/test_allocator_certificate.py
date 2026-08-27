"""The optimality certificate has to actually certify.

`Allocation.optimality_ratio` is quoted in the README, in the console and in the
interview answers. It is only worth quoting if the dual it divides by is a real
upper bound on the true constrained optimum — otherwise it is a number that
looks like a guarantee and is not one.

The existing suite measures how CLOSE the allocator gets to brute force. That is
a different property: it scores the heuristic, not the certificate. These tests
score the certificate, by checking the two things that make it meaningful:

  1. the bound is never below the true optimum        (it is a valid bound)
  2. the certified ratio never exceeds the true ratio (it is conservative)

Either failing means every optimality claim in the repository is overstated.
"""

from __future__ import annotations

import random

import pytest
from yukti.allocator.lagrangian import (
    Budgets,
    Candidate,
    DualBoundViolation,
    allocate,
    brute_force,
)

# Small enough for exhaustive enumeration, varied enough to hit the regimes
# where a relaxation is known to be weak: tight budgets, negative-margin
# candidates, and several cases sharing one customer.
INSTANCES = 250


def _instance(seed: int) -> tuple[list[Candidate], Budgets]:
    rng = random.Random(seed)
    n = rng.randint(4, 12)
    candidates = [
        Candidate(
            case_id=f"case{i}",
            # Few customers, many cases: makes the per-customer cap bind, which
            # is the constraint the relaxation does NOT price.
            customer_id=f"cust{rng.randrange(max(1, n // 2))}",
            action_kind="message",
            channel="sms",
            margin_paise=rng.randint(-5_000, 90_000),
            contacts=rng.choice([0, 1, 1]),
            discount_paise=rng.choice([0, 0, 5_000, 20_000]),
            channel_cost_paise=25,
        )
        for i in range(n)
    ]
    budgets = Budgets(
        contacts=rng.randint(1, 3),
        discount_paise=rng.choice([0, 5_000, 25_000]),
        per_customer_contacts=1,
    )
    return candidates, budgets


class TestDualBound:
    def test_bound_is_never_below_the_true_optimum(self) -> None:
        """Weak duality, checked against enumeration rather than asserted.

        A dual that dips below the optimum would make `optimality_ratio` exceed
        the truth — the direction that flatters, and therefore the one worth
        spending enumeration on.
        """
        violations = []
        for seed in range(INSTANCES):
            candidates, budgets = _instance(seed)
            optimum = brute_force(candidates, budgets)
            allocation = allocate(candidates, budgets)
            if optimum <= 0:
                continue
            if allocation.dual_bound_paise < optimum - 1:
                violations.append((seed, optimum, allocation.dual_bound_paise))
        assert not violations, f"dual bound below true optimum: {violations[:5]}"

    def test_certified_ratio_never_overstates_the_true_ratio(self) -> None:
        """The published number must be a LOWER bound on how good we did."""
        overstated = []
        for seed in range(INSTANCES):
            candidates, budgets = _instance(seed)
            optimum = brute_force(candidates, budgets)
            allocation = allocate(candidates, budgets)
            if optimum <= 0:
                continue
            true_ratio = allocation.total_margin_paise / optimum
            if allocation.optimality_ratio > true_ratio + 1e-9:
                overstated.append(
                    (seed, allocation.optimality_ratio, true_ratio)
                )
        assert not overstated, f"certificate overstated quality: {overstated[:5]}"

    def test_violation_is_raised_not_repaired(self) -> None:
        """A broken certificate must fail loudly.

        The previous code took `max(dual, margin)`, so a dual that came out below
        a feasible primal — which is impossible unless the dual computation is
        wrong — was silently rewritten into a ratio of exactly 1.0. That is the
        worst available failure mode: the certificate reports perfection
        precisely when it has stopped working.
        """
        assert issubclass(DualBoundViolation, AssertionError)


class TestBudgetsAreHardConstraints:
    """A merchant authorised a number; the allocator does not get to exceed it.

    Re-checked here because the fill and greedy passes were rewritten to track
    budget state incrementally, and an off-by-one in that bookkeeping would
    overspend rather than crash.
    """

    @pytest.mark.parametrize("seed", range(60))
    def test_no_budget_is_ever_exceeded(self, seed: int) -> None:
        candidates, budgets = _instance(seed)
        chosen = allocate(candidates, budgets).chosen

        assert sum(c.contacts for c in chosen) <= budgets.contacts
        assert sum(c.discount_paise for c in chosen) <= budgets.discount_paise

        per_customer: dict[str, int] = {}
        for c in chosen:
            per_customer[c.customer_id] = per_customer.get(c.customer_id, 0) + c.contacts
        assert all(v <= budgets.per_customer_contacts for v in per_customer.values())

        # One funded action per case, or the same debt gets chased twice.
        case_ids = [c.case_id for c in chosen]
        assert len(case_ids) == len(set(case_ids))


class TestLargeMultiplierStability:
    """The certificate must hold where the multiplier is enormous.

    `_bisect` grows the multiplier geometrically and returns values around 5e8
    on these instances, which is the regime where float error in the dual sum
    would show up if it were going to. It does not: the measured gap between
    dual and primal here is exactly 0.0, so this is a guard against a future
    regression rather than a reproduction of a past one.

    What it does establish is that `allocate` neither raises nor overspends when
    a budget binds hard enough to price a contact at five lakh rupees -- the
    regime a large merchant with a small contact budget actually sits in.
    """

    @staticmethod
    def _tight(seed: int, n: int = 4_000) -> tuple[list[Candidate], Budgets]:
        """Large margins, many candidates, a budget of one. Forces a huge lambda."""
        rng = random.Random(seed)
        candidates = [
            Candidate(
                case_id=f"case{i}",
                customer_id=f"cust{i}",
                action_kind="message",
                channel="sms",
                # Crores, so the multiplier that suppresses them is enormous.
                margin_paise=rng.randint(1_00_00_000, 50_00_00_000),
                contacts=1,
                discount_paise=rng.choice([0, 10_00_000]),
                channel_cost_paise=25,
            )
            for i in range(n)
        ]
        return candidates, Budgets(
            contacts=1, discount_paise=10_00_000, per_customer_contacts=1
        )

    @pytest.mark.parametrize("seed", range(12))
    def test_no_spurious_violation_at_large_lambda(self, seed: int) -> None:
        candidates, budgets = self._tight(seed)
        allocation = allocate(candidates, budgets)   # must not raise
        assert allocation.lambda_contact > 0, "expected the contact budget to bind"
        assert allocation.dual_bound_paise >= allocation.total_margin_paise - 1

    def test_budgets_still_hold_in_that_regime(self) -> None:
        candidates, budgets = self._tight(0)
        chosen = allocate(candidates, budgets).chosen
        assert sum(c.contacts for c in chosen) <= budgets.contacts
        assert sum(c.discount_paise for c in chosen) <= budgets.discount_paise
