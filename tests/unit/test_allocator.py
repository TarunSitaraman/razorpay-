"""Budget-constrained allocation.

Three things must hold, in this order of importance:
  1. budgets and caps are NEVER exceeded — the merchant authorised a number;
  2. the objective is incremental margin, so persuadables beat sure things;
  3. the result is close to optimal, measured against a brute-force oracle
     rather than against the relaxation's own bound.
"""

from __future__ import annotations

import random

import pytest
from yukti.allocator.lagrangian import (
    Allocation,
    Budgets,
    Candidate,
    allocate,
    brute_force,
    expected_margin,
)


def cand(
    i: int, margin: int, *, customer: str = "cus_1", contacts: int = 1,
    discount: int = 0, kind: str = "message",
) -> Candidate:
    return Candidate(
        case_id=f"case_{i}", customer_id=customer, action_kind=kind,
        channel="whatsapp" if contacts else "none", margin_paise=margin,
        contacts=contacts, discount_paise=discount,
        channel_cost_paise=75 if contacts else 0,
    )


def random_instance(rng: random.Random) -> tuple[list[Candidate], Budgets]:
    n = rng.randint(6, 14)
    candidates = []
    for i in range(n):
        contacts = rng.choice([0, 1, 1, 1])
        discount = rng.choice([0, 0, 5_000, 20_000]) if contacts else 0
        candidates.append(cand(
            i, rng.randint(-2_000, 60_000),
            customer=f"cus_{rng.randint(0, max(1, n // 3))}",
            contacts=contacts, discount=discount,
        ))
    budgets = Budgets(
        contacts=rng.randint(1, max(2, n // 2)),
        discount_paise=rng.choice([0, 10_000, 40_000, 100_000]),
        per_customer_contacts=rng.randint(1, 2),
    )
    return candidates, budgets


class TestObjective:
    def test_margin_uses_uplift_not_propensity(self):
        """The single substitution that would make this an ordinary tool.

        A 70%-likely recovery with 2% causal effect must be worth far less than
        a 25%-likely recovery with 20% causal effect.
        """
        sure_thing = expected_margin(uplift=0.02, amount_paise=100_000, mdr_bps=200)
        persuadable = expected_margin(uplift=0.20, amount_paise=100_000, mdr_bps=200)
        assert persuadable > 5 * sure_thing

    def test_costs_are_subtracted_in_full(self):
        # Paid whether or not the recovery lands.
        bare = expected_margin(0.2, 100_000, 200)
        costed = expected_margin(0.2, 100_000, 200, discount_paise=5_000,
                                 channel_cost_paise=75)
        assert costed == bare - 5_075

    def test_mdr_reduces_margin(self):
        assert expected_margin(0.2, 100_000, 0) > expected_margin(0.2, 100_000, 300)

    def test_margin_can_be_negative(self):
        # An expensive channel on a low-uplift case genuinely loses money, and
        # the allocator needs to see that rather than a floor at zero.
        assert expected_margin(0.001, 10_000, 200, channel_cost_paise=900) < 0


class TestHardConstraints:
    """Budgets are authorisations, not suggestions."""

    def test_contact_budget_is_never_exceeded(self):
        cands = [cand(i, 50_000, customer=f"cus_{i}") for i in range(10)]
        result = allocate(cands, Budgets(contacts=3, discount_paise=0,
                                         per_customer_contacts=5))
        assert result.contacts_used <= 3

    def test_discount_budget_is_never_exceeded(self):
        cands = [cand(i, 50_000, customer=f"cus_{i}", discount=20_000) for i in range(10)]
        result = allocate(cands, Budgets(contacts=100, discount_paise=50_000,
                                         per_customer_contacts=5))
        assert result.discount_used_paise <= 50_000

    def test_at_most_one_action_per_case(self):
        # Funding two actions on one obligation would double-contact for a
        # single debt.
        cands = [
            Candidate("case_1", "cus_1", "message", "whatsapp", 40_000, 1, 0, 75),
            Candidate("case_1", "cus_1", "voice_call", "voice", 50_000, 1, 0, 900),
        ]
        result = allocate(cands, Budgets(contacts=10, discount_paise=0,
                                         per_customer_contacts=10))
        assert len({c.case_id for c in result.chosen}) == len(result.chosen)

    @pytest.mark.parametrize("seed", range(40))
    def test_no_constraint_is_ever_violated(self, seed):
        rng = random.Random(seed)
        cands, budgets = random_instance(rng)
        result = allocate(cands, budgets)

        assert result.contacts_used <= budgets.contacts
        assert result.discount_used_paise <= budgets.discount_paise

        per_customer: dict[str, int] = {}
        for c in result.chosen:
            per_customer[c.customer_id] = per_customer.get(c.customer_id, 0) + c.contacts
        assert all(v <= budgets.per_customer_contacts for v in per_customer.values())


class TestCrossSurfaceArbitration:
    """The reason this layer exists at all."""

    def test_one_customer_with_many_cases_is_capped_once(self):
        # Cart, subscription and invoice for the same person — a per-agent
        # budget would happily fund all three.
        cands = [
            cand(1, 40_000, customer="cus_A", kind="cart"),
            cand(2, 38_000, customer="cus_A", kind="subscription"),
            cand(3, 36_000, customer="cus_A", kind="invoice"),
        ]
        result = allocate(cands, Budgets(contacts=10, discount_paise=0,
                                         per_customer_contacts=1))
        assert sum(c.contacts for c in result.chosen) == 1

    def test_the_most_valuable_case_wins_the_slot(self):
        cands = [
            cand(1, 10_000, customer="cus_A"),
            cand(2, 90_000, customer="cus_A"),
            cand(3, 50_000, customer="cus_A"),
        ]
        result = allocate(cands, Budgets(contacts=5, discount_paise=0,
                                         per_customer_contacts=1))
        assert [c.case_id for c in result.chosen] == ["case_2"]

    def test_free_actions_bypass_the_contact_cap(self):
        # A silent retry does not touch the customer, so it must not consume
        # their attention budget.
        cands = [
            cand(1, 20_000, customer="cus_A", contacts=0, kind="silent_retry"),
            cand(2, 20_000, customer="cus_A", contacts=0, kind="silent_retry"),
            cand(3, 20_000, customer="cus_A", contacts=1),
        ]
        result = allocate(cands, Budgets(contacts=1, discount_paise=0,
                                         per_customer_contacts=1))
        assert len(result.chosen) == 3


class TestOptimality:
    @pytest.mark.parametrize("seed", range(60))
    def test_within_five_percent_of_a_true_optimum(self, seed):
        """Measured against brute force, not against the relaxation's own bound.

        The Lagrangian dual would flatter the result; an exact enumeration
        cannot.
        """
        rng = random.Random(1000 + seed)
        cands, budgets = random_instance(rng)
        result = allocate(cands, budgets)
        optimum = brute_force(cands, budgets)
        if optimum <= 0:
            return
        assert result.total_margin_paise >= 0.95 * optimum

    def test_negative_margin_candidates_are_never_funded(self):
        cands = [cand(i, -5_000, customer=f"cus_{i}") for i in range(5)]
        result = allocate(cands, Budgets(contacts=10, discount_paise=100_000,
                                         per_customer_contacts=5))
        assert result.chosen == []

    def test_leftover_budget_is_spent(self):
        # Unspent budget is forgone margin.
        cands = [cand(i, 10_000, customer=f"cus_{i}") for i in range(5)]
        result = allocate(cands, Budgets(contacts=5, discount_paise=0,
                                         per_customer_contacts=1))
        assert result.contacts_used == 5

    def test_optimality_ratio_is_reported(self):
        rng = random.Random(3)
        cands, budgets = random_instance(rng)
        assert 0.0 <= allocate(cands, budgets).optimality_ratio <= 1.0


class TestShadowPrices:
    def test_unbinding_budget_is_priced_at_zero(self):
        # Pricing a resource nobody is competing for would suppress good actions
        # for no reason.
        cands = [cand(1, 50_000)]
        result = allocate(cands, Budgets(contacts=100, discount_paise=1_000_000,
                                         per_customer_contacts=10))
        assert result.lambda_contact == 0.0

    def test_scarce_contacts_acquire_a_positive_price(self):
        cands = [cand(i, 50_000, customer=f"cus_{i}") for i in range(40)]
        result = allocate(cands, Budgets(contacts=2, discount_paise=0,
                                         per_customer_contacts=1))
        # The shadow price is what one more contact would be worth — directly
        # reportable to a merchant deciding whether to raise the budget.
        assert result.lambda_contact > 0


class TestEdgeCases:
    def test_no_candidates(self):
        assert allocate([], Budgets(10, 10_000, 3)) == Allocation()

    def test_zero_budget_funds_only_free_actions(self):
        cands = [
            cand(1, 40_000, contacts=1),
            cand(2, 30_000, contacts=0, kind="silent_retry"),
        ]
        result = allocate(cands, Budgets(contacts=0, discount_paise=0,
                                         per_customer_contacts=0))
        assert [c.case_id for c in result.chosen] == ["case_2"]

    def test_brute_force_refuses_large_instances(self):
        with pytest.raises(ValueError):
            brute_force([cand(i, 1) for i in range(21)], Budgets(1, 1, 1))
