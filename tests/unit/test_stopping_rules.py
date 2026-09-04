"""Stopping rules — the half of the track's bar that decides what NOT to chase.

Every branch here decides whether real money gets spent, so each rule is tested
alone as well as through the composed evaluator.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from yukti.domain.enums import ObligationState, StopReason
from yukti.stopping.rules import (
    DIMINISHING_RETURNS_DAYS,
    MIN_EXPECTED_MARGIN_PAISE,
    CaseSnapshot,
    StopDecision,
    contact_budget_spent,
    customer_opted_out,
    diminishing_returns,
    discount_budget_spent,
    evaluate,
    issuer_degraded,
    lost_cause,
    negative_expected_margin,
    npci_cap_reached,
    obligation_resolved,
    open_promise_to_pay,
    rule_ids,
)

NOW = datetime(2026, 6, 1, 10, 0)


def snap(**over) -> CaseSnapshot:
    """A healthy case that every rule lets through, so a test changes one thing."""
    base = dict(
        case_id="case_1",
        obligation_state=ObligationState.OPEN,
        decline_code="INSUFFICIENT_FUNDS",
        first_failed_at=NOW - timedelta(days=2),
        attempts_made=1,
        customer_opted_out=False,
        open_promise_to_pay=False,
        issuer_degraded=False,
        contacts_this_window=0,
        contact_cap=3,
        contact_budget_remaining=50,
        discount_budget_remaining_paise=100_000,
        predicted_uplift=0.12,
        expected_margin_paise=25_000,
        requires_discount=False,
    )
    return CaseSnapshot(**{**base, **over})


class TestDecisionInvariant:
    def test_a_stop_must_name_a_rule(self):
        # The brief asks for stopping rules by name. A stop with no reason would
        # show up in the console as an unexplained blank.
        with pytest.raises(ValueError, match="named StopReason"):
            StopDecision(stop=True, reason=None)

    def test_a_continue_must_not_carry_a_reason(self):
        with pytest.raises(ValueError):
            StopDecision(stop=False, reason=StopReason.LOST_CAUSE)

    def test_healthy_case_is_not_stopped(self):
        assert evaluate(snap(), NOW).stop is False


class TestIndividualRules:
    def test_resolved_obligation_stops(self):
        d = obligation_resolved(snap(obligation_state=ObligationState.RECOVERED))
        assert d.stop and d.reason is StopReason.OBLIGATION_RESOLVED

    def test_opt_out_stops_globally(self):
        d = customer_opted_out(snap(customer_opted_out=True))
        assert d.stop and d.reason is StopReason.CUSTOMER_OPTED_OUT

    def test_open_promise_stops(self):
        d = open_promise_to_pay(snap(open_promise_to_pay=True))
        assert d.stop and d.reason is StopReason.OPEN_PROMISE_TO_PAY

    @pytest.mark.parametrize(
        "code", ["MANDATE_REVOKED", "ACCOUNT_CLOSED", "CARD_BLOCKED", "AP08"]
    )
    def test_permanent_declines_are_lost_causes(self, code):
        d = lost_cause(snap(decline_code=code))
        assert d.stop and d.reason is StopReason.LOST_CAUSE

    def test_near_zero_uplift_is_a_lost_cause(self):
        # Catches customers the model has learned are gone even when the decline
        # code still looks retryable.
        d = lost_cause(snap(predicted_uplift=0.001))
        assert d.stop and d.reason is StopReason.LOST_CAUSE

    def test_npci_cap_comes_from_the_shared_decline_table(self):
        from yukti.domain.decline import lookup

        cap = lookup("AP39").max_attempts
        assert npci_cap_reached(snap(decline_code="AP39", attempts_made=cap - 1)).stop is False
        d = npci_cap_reached(snap(decline_code="AP39", attempts_made=cap))
        assert d.stop and d.reason is StopReason.NPCI_REPRESENT_CAP

    def test_degraded_issuer_suppresses(self):
        d = issuer_degraded(snap(issuer_degraded=True))
        assert d.stop and d.reason is StopReason.ISSUER_DEGRADED

    def test_diminishing_returns_after_the_knee(self):
        old = snap(first_failed_at=NOW - timedelta(days=DIMINISHING_RETURNS_DAYS + 1))
        d = diminishing_returns(old, NOW)
        assert d.stop and d.reason is StopReason.DIMINISHING_RETURNS

    def test_fresh_case_is_not_past_the_knee(self):
        assert diminishing_returns(snap(), NOW).stop is False


class TestCrossSurfaceContactCap:
    """The cross-agent arbitration constraint, which is the point of the layer."""

    def test_customer_cap_counts_contacts_from_every_surface(self):
        # Three separate agents each sending one "reasonable" message is exactly
        # the failure a per-agent budget cannot see.
        d = contact_budget_spent(snap(contacts_this_window=3, contact_cap=3))
        assert d.stop and d.reason is StopReason.CONTACT_BUDGET_SPENT
        assert "across all surfaces" in d.detail

    def test_under_the_cap_continues(self):
        assert contact_budget_spent(snap(contacts_this_window=2, contact_cap=3)).stop is False

    def test_merchant_pool_exhaustion_also_stops(self):
        d = contact_budget_spent(snap(contact_budget_remaining=0))
        assert d.stop and d.reason is StopReason.CONTACT_BUDGET_SPENT


class TestBudgetAndMargin:
    def test_discount_budget_only_binds_when_a_discount_is_needed(self):
        no_discount = snap(discount_budget_remaining_paise=0, requires_discount=False)
        assert discount_budget_spent(no_discount).stop is False

        needs = snap(discount_budget_remaining_paise=0, requires_discount=True)
        d = discount_budget_spent(needs)
        assert d.stop and d.reason is StopReason.DISCOUNT_BUDGET_SPENT

    def test_margin_below_the_floor_stops(self):
        d = negative_expected_margin(snap(expected_margin_paise=MIN_EXPECTED_MARGIN_PAISE - 1))
        assert d.stop and d.reason is StopReason.NEGATIVE_EXPECTED_MARGIN

    def test_negative_margin_stops(self):
        d = negative_expected_margin(snap(expected_margin_paise=-5_000))
        assert d.stop and d.reason is StopReason.NEGATIVE_EXPECTED_MARGIN

    def test_margin_at_the_floor_continues(self):
        assert negative_expected_margin(
            snap(expected_margin_paise=MIN_EXPECTED_MARGIN_PAISE)
        ).stop is False


class TestPrecedence:
    def test_most_fundamental_reason_wins(self):
        """A revoked mandate with an exhausted budget must read LOST_CAUSE.

        The merchant needs to know the money is gone, not that we ran out of
        messages — the second is fixable by raising a budget, the first is not.
        """
        d = evaluate(
            snap(decline_code="MANDATE_REVOKED", contact_budget_remaining=0,
                 contacts_this_window=99),
            NOW,
        )
        assert d.reason is StopReason.LOST_CAUSE

    def test_opt_out_outranks_economics(self):
        # Consent withdrawal is binding regardless of how profitable the case is.
        d = evaluate(snap(customer_opted_out=True, expected_margin_paise=10_000_000), NOW)
        assert d.reason is StopReason.CUSTOMER_OPTED_OUT

    def test_resolved_outranks_everything(self):
        d = evaluate(
            snap(obligation_state=ObligationState.RECOVERED, customer_opted_out=True), NOW
        )
        assert d.reason is StopReason.OBLIGATION_RESOLVED


class TestCoverage:
    def test_every_stop_reason_is_reachable(self):
        """No StopReason may be decorative.

        A reason the code can never produce would appear in the console legend
        and never in the data, which is worse than not having it.
        """
        produced = set()
        cases = [
            snap(obligation_state=ObligationState.RECOVERED),
            snap(customer_opted_out=True),
            snap(open_promise_to_pay=True),
            snap(decline_code="MANDATE_REVOKED"),
            snap(decline_code="AP39", attempts_made=9),
            snap(issuer_degraded=True),
            snap(first_failed_at=NOW - timedelta(days=60)),
            snap(contacts_this_window=5, contact_cap=3),
            snap(requires_discount=True, discount_budget_remaining_paise=0),
            snap(expected_margin_paise=-1),
        ]
        for c in cases:
            d = evaluate(c, NOW)
            if d.stop:
                produced.add(d.reason)

        # Reasons a human produces rather than a rule. Named individually, and
        # subtracted rather than skipped, so a reason that no code path at all
        # can reach still fails this test. HUMAN_REJECTED is written by
        # `yukti.approvals.decide` and covered by
        # tests/integration/test_approvals_api.py.
        BY_A_HUMAN = {StopReason.HUMAN_REJECTED}

        assert produced == set(StopReason) - BY_A_HUMAN, (
            f"unreachable stop reasons: "
            f"{set(StopReason) - BY_A_HUMAN - produced}"
        )

    def test_rule_ids_match_the_enum(self):
        assert set(rule_ids()) == {r.value for r in StopReason}
