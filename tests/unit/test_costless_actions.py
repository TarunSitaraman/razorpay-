"""A free, unseen action is never declined on an estimate or on a budget.

This invariant was violated in four separate places before it was written down,
and each violation looked like a different bug:

  * `allocator.lagrangian`      funded only `margin > 0`
  * `stopping.negative_expected_margin`  stopped below a margin floor
  * `stopping.lost_cause`       stopped on near-zero predicted uplift
  * `stopping.contact_budget_spent`      stopped when the contact pool ran dry

They share a cause. A silent retry costs nothing and the customer never sees it,
so its true effect is small but strictly non-negative — it has no downside
branch to trigger. That makes `margin > 0` a test of the *estimate's sign*, and
near zero the sign of a noisy estimate is close to a coin flip. Every one of
those four declined free money on that coin flip, and together they were the
whole reason Yukti lost its own evaluation to a fixed cadence.

So the property is asserted once, over the rules as a set, rather than four
times in four files. A fifth rule that reaches for the same shortcut fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yukti.allocator.lagrangian import Budgets, Candidate, allocate
from yukti.domain.enums import ObligationState, StopReason
from yukti.stopping.rules import CaseSnapshot
from yukti.stopping.rules import evaluate as evaluate_stopping

NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


def snapshot(**over) -> CaseSnapshot:
    base = dict(
        case_id="case_1",
        obligation_state=ObligationState.OPEN,
        decline_code="INSUFFICIENT_FUNDS",
        first_failed_at=NOW - timedelta(days=2),
        attempts_made=0,
        customer_opted_out=False,
        open_promise_to_pay=False,
        issuer_degraded=False,
        contacts_this_window=0,
        contact_cap=3,
        contact_budget_remaining=50,
        discount_budget_remaining_paise=100_000,
        predicted_uplift=0.20,
        expected_margin_paise=50_000,
        has_costless_action=True,
    )
    base.update(over)
    return CaseSnapshot(**base)


# Conditions that must NOT close a case while a free retry is still available.
# Each is an economic or budgetary judgement, and none of them applies to an
# action that spends nothing.
SURVIVABLE = {
    "no contact budget left": {"contact_budget_remaining": 0},
    "customer at their contact cap": {"contacts_this_window": 3, "contact_cap": 3},
    "estimated margin below the floor": {"expected_margin_paise": 0},
    "estimated margin negative": {"expected_margin_paise": -90_000},
    "predicted uplift indistinguishable from zero": {"predicted_uplift": 0.0001},
    "predicted uplift negative": {"predicted_uplift": -0.02},
    "all of them at once": {
        "contact_budget_remaining": 0, "contacts_this_window": 3,
        "expected_margin_paise": -90_000, "predicted_uplift": -0.02,
    },
}


@pytest.mark.parametrize("label", list(SURVIVABLE))
def test_costless_action_survives_economic_stops(label):
    decision = evaluate_stopping(snapshot(**SURVIVABLE[label]), NOW)
    assert not decision.stop, (
        f"a free silent retry was stopped by {decision.reason} because {label} — "
        "none of those costs applies to an action that spends nothing"
    )


@pytest.mark.parametrize("label", list(SURVIVABLE))
def test_the_same_case_without_a_free_action_does_stop(label):
    """The mirror, so the test above cannot pass by nothing ever stopping."""
    decision = evaluate_stopping(
        snapshot(has_costless_action=False, **SURVIVABLE[label]), NOW)
    assert decision.stop and decision.reason is not None


# Conditions that SHOULD still close the case: a free retry does not help, or is
# not permitted. Guarding these too would have been the opposite mistake.
@pytest.mark.parametrize("over,reason", [
    ({"decline_code": "MANDATE_REVOKED"}, StopReason.LOST_CAUSE),
    ({"attempts_made": 99}, StopReason.NPCI_REPRESENT_CAP),
    ({"issuer_degraded": True}, StopReason.ISSUER_DEGRADED),
    ({"customer_opted_out": True}, StopReason.CUSTOMER_OPTED_OUT),
    ({"obligation_state": ObligationState.RECOVERED}, StopReason.OBLIGATION_RESOLVED),
])
def test_a_free_action_does_not_override_a_real_stop(over, reason):
    decision = evaluate_stopping(snapshot(**over), NOW)
    assert decision.stop and decision.reason is reason


def _cand(kind: str, margin: int, *, contacts: int = 0, cost: int = 0) -> Candidate:
    return Candidate(
        case_id="case_1", customer_id="cus_1", action_kind=kind,
        channel="whatsapp" if contacts else "none", margin_paise=margin,
        contacts=contacts, discount_paise=0, channel_cost_paise=cost,
    )


def test_allocator_funds_a_costless_action_the_model_scored_negative():
    """The allocator's half of the same invariant."""
    alloc = allocate([_cand("silent_retry", -4_000)],
                     Budgets(contacts=10, discount_paise=0, per_customer_contacts=1))
    kinds = {c.action_kind for c in alloc.chosen}
    assert kinds == {"silent_retry"}
    # And it is accounted outside the budgeted objective, so the optimality
    # certificate still describes the knapsack it is a certificate of.
    assert alloc.total_margin_paise == 0
    assert alloc.costless_margin_paise == -4_000


def test_allocator_still_declines_a_negative_contact():
    """A contact is seen and paid for, so its sign genuinely does decide."""
    alloc = allocate([_cand("message", -4_000, contacts=1, cost=75)],
                     Budgets(contacts=10, discount_paise=0, per_customer_contacts=1))
    assert alloc.chosen == []


def test_a_funded_action_is_not_displaced_by_the_costless_one():
    """At most one action per case: the costless pass fills gaps, never competes."""
    alloc = allocate(
        [_cand("message", 900_000, contacts=1, cost=75), _cand("silent_retry", -4_000)],
        Budgets(contacts=10, discount_paise=0, per_customer_contacts=1),
    )
    assert [c.action_kind for c in alloc.chosen] == ["message"]
