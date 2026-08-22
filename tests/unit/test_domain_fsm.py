"""Property tests for the domain state machines and money arithmetic.

These are the invariants the rest of the system assumes without re-checking, so
they are tested exhaustively rather than by example.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from yukti.domain import money
from yukti.domain.decline import BY_CODE, lookup
from yukti.domain.enums import (
    ActionKind,
    CaseState,
    ObligationState,
    Transience,
)
from yukti.domain.fsm import (
    IllegalTransition,
    assert_case_transition,
    can_transition_case,
    check_version,
)

ALL_CASE_STATES = list(CaseState)


class TestCaseFsm:
    @pytest.mark.parametrize("terminal", [CaseState.STOPPED, CaseState.RECOVERED, CaseState.LOST])
    @pytest.mark.parametrize("dst", ALL_CASE_STATES)
    def test_terminal_states_have_no_outgoing_edges(self, terminal, dst):
        # A stopped case must never silently resume, and a recovered obligation
        # must not reopen because a stale event arrived late.
        assert not can_transition_case(terminal, dst)

    @pytest.mark.parametrize("src", ALL_CASE_STATES)
    def test_no_self_transitions(self, src):
        assert not can_transition_case(src, src)

    @pytest.mark.parametrize("src", ALL_CASE_STATES)
    def test_every_state_can_reach_recovered_or_is_terminal(self, src):
        # Recovery can arrive at any moment via an organic payment, so every
        # live state must accept it. Otherwise a real payment would be rejected.
        assert src.is_terminal or can_transition_case(src, CaseState.RECOVERED)

    @pytest.mark.parametrize("src", ALL_CASE_STATES)
    def test_every_live_state_can_stop(self, src):
        # Stopping rules must be able to fire from any live state; a state that
        # cannot stop is a state where budget can be burned unboundedly.
        assert src.is_terminal or can_transition_case(src, CaseState.STOPPED)

    def test_illegal_transition_raises(self):
        with pytest.raises(IllegalTransition):
            assert_case_transition(CaseState.RECOVERED, CaseState.PLANNING)


class TestVersioning:
    @given(current=st.integers(0, 10_000), delta=st.integers(1, 1_000))
    def test_newer_events_apply(self, current, delta):
        assert check_version(current, current + delta).apply

    @given(v=st.integers(0, 10_000))
    def test_same_version_is_a_duplicate_not_an_error(self, v):
        # At-least-once delivery makes redelivery ordinary. It must be a no-op,
        # not a failure that lands an otherwise-valid message in the DLQ.
        r = check_version(v, v)
        assert not r.apply and not r.superseded

    @given(current=st.integers(1, 10_000), delta=st.integers(1, 1_000))
    def test_older_events_are_superseded_never_applied(self, current, delta):
        r = check_version(current, max(0, current - delta))
        assert not r.apply and r.superseded


class TestMoney:
    @given(st.integers(-10**12, 10**12))
    def test_rupee_roundtrip_is_lossless(self, paise):
        assert money.rupees_to_paise(money.paise_to_rupees(paise)) == paise

    def test_no_float_drift_on_repeated_small_discounts(self):
        # The failure this guards against: summing a million 0.1-rupee discounts
        # in float drifts. In integer paise it cannot.
        total = sum(money.rupees_to_paise("0.10") for _ in range(1_000_000))
        assert total == 10_000_000

    @pytest.mark.parametrize(
        "paise,expected",
        [
            (0, "₹0.00"),
            (100, "₹1.00"),
            (123456789, "₹12,34,567.89"),   # Indian grouping: 3 then 2s
            (100000000, "₹10,00,000.00"),
            (-50050, "-₹500.50"),
        ],
    )
    def test_indian_digit_grouping(self, paise, expected):
        assert money.format_inr(paise) == expected

    @given(amount=st.integers(1, 10**10), pct=st.integers(0, 100))
    def test_discount_never_exceeds_amount(self, amount, pct):
        assert 0 <= money.apply_discount_pct(amount, pct) <= amount

    def test_discount_pct_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            money.apply_discount_pct(1000, 101)
        with pytest.raises(ValueError):
            money.apply_discount_pct(1000, -1)

    def test_half_up_rounding_at_the_paisa(self):
        assert money.rupees_to_paise(Decimal("0.005")) == 1


class TestActionSchemaIsBounded:
    def test_no_money_exfiltrating_action_exists(self):
        # The guarantee is absence, not runtime denial: if the model cannot name
        # the action, no prompt injection can request it.
        names = {a.value for a in ActionKind}
        forbidden = {"refund", "payout", "settlement", "cancel_mandate",
                     "transfer", "chargeback", "adjust_balance"}
        assert names & forbidden == set()

    def test_only_two_action_kinds_can_move_money(self):
        movers = {a for a in ActionKind if a.moves_money}
        assert movers == {ActionKind.SILENT_RETRY, ActionKind.SCHEDULE_DEBIT}


class TestDeclineTaxonomy:
    def test_unknown_code_degrades_conservatively(self):
        spec = lookup("SOME_CODE_WE_HAVE_NEVER_SEEN")
        assert spec.transience is Transience.UNCLASSIFIED
        # Conservative means: at most one cheap attempt, never an aggressive one.
        assert spec.max_attempts == 1

    def test_none_and_empty_are_safe(self):
        assert lookup(None).transience is Transience.UNCLASSIFIED
        assert lookup("").transience is Transience.UNCLASSIFIED

    def test_lookup_is_case_and_whitespace_insensitive(self):
        assert lookup("  mandate_revoked  ").code == "MANDATE_REVOKED"

    @pytest.mark.parametrize("code", sorted(BY_CODE))
    def test_permanent_failures_are_never_retryable(self, code):
        spec = BY_CODE[code]
        if spec.transience is Transience.PERMANENT:
            # Retrying a revoked mandate is both wasted spend and a compliance risk.
            assert spec.max_attempts == 0
            assert not spec.retryable_silently

    @pytest.mark.parametrize("code", sorted(BY_CODE))
    def test_pure_system_failures_are_not_customer_actionable(self, code):
        spec = BY_CODE[code]
        if spec.transience is Transience.TRANSIENT_SYSTEM:
            # Messaging a customer during issuer downtime burns a contact and
            # tells them the merchant is broken. The correct action is to wait.
            assert not spec.customer_actionable
