"""Attribution correctness — the measurement everything else depends on."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from yukti.domain.ids import action_id, case_id, decision_id, new_id
from yukti.experiment.attribution import (
    DEFAULT_ATTRIBUTION_WINDOW_H,
    attribute_capture,
    record_outcome,
)

pytestmark = pytest.mark.integration

CAPTURED_AT = datetime(2026, 5, 20, 12, 0)


@pytest.fixture
def case(conn, merchant, customer, obligation):
    cid = case_id()
    conn.execute(
        "INSERT INTO recovery_case (id, obligation_id, merchant_id, customer_id, state, arm) "
        "VALUES (%s, %s, %s, %s, 'awaiting_outcome', 'treatment')",
        (cid, obligation, merchant, customer),
    )
    return cid


def add_action(conn, case_id_, dispatched_at, status="dispatched"):
    did, aid = decision_id(), action_id()
    conn.execute(
        "INSERT INTO agent_decision (id, case_id, trace_id, action_kind, reason, policy_verdict) "
        "VALUES (%s, %s, 'tr', 'message', 'test', 'allow')",
        (did, case_id_),
    )
    conn.execute(
        "INSERT INTO recovery_action "
        "(id, decision_id, case_id, kind, idempotency_key, dispatched_at, status) "
        "VALUES (%s, %s, %s, 'message', %s, %s, %s)",
        (aid, did, case_id_, new_id("idem"), dispatched_at, status),
    )
    return aid


class TestWindow:
    def test_capture_inside_the_window_is_attributed(self, conn, obligation, case):
        aid = add_action(conn, case, CAPTURED_AT - timedelta(hours=4))
        attr = attribute_capture(conn, obligation, CAPTURED_AT, 250_000)
        assert attr is not None and attr.action_id == aid and not attr.is_organic

    def test_capture_outside_the_window_is_organic(self, conn, obligation, case):
        # An action from a week ago did not cause today's payment. Crediting it
        # is how gross-recovery numbers get inflated.
        add_action(conn, case, CAPTURED_AT - timedelta(hours=DEFAULT_ATTRIBUTION_WINDOW_H + 5))
        attr = attribute_capture(conn, obligation, CAPTURED_AT, 250_000)
        assert attr is not None and attr.is_organic

    def test_capture_before_the_action_is_organic(self, conn, obligation, case):
        # An action dispatched after the capture cannot have caused it.
        add_action(conn, case, CAPTURED_AT + timedelta(hours=1))
        attr = attribute_capture(conn, obligation, CAPTURED_AT, 250_000)
        assert attr.is_organic

    def test_most_recent_eligible_action_wins(self, conn, obligation, case):
        add_action(conn, case, CAPTURED_AT - timedelta(hours=40))
        recent = add_action(conn, case, CAPTURED_AT - timedelta(hours=2))
        attr = attribute_capture(conn, obligation, CAPTURED_AT, 250_000)
        assert attr.action_id == recent

    def test_undispatched_action_is_not_credited(self, conn, obligation, case):
        # A planned-but-blocked action never reached the customer, so it cannot
        # have caused anything.
        add_action(conn, case, CAPTURED_AT - timedelta(hours=2), status="pending")
        attr = attribute_capture(conn, obligation, CAPTURED_AT, 250_000)
        assert attr.is_organic


class TestNoActionsAtAll:
    def test_case_with_no_actions_is_organic(self, conn, obligation, case):
        attr = attribute_capture(conn, obligation, CAPTURED_AT, 250_000)
        assert attr is not None and attr.is_organic

    def test_unknown_obligation_returns_none(self, conn):
        assert attribute_capture(conn, "obl_nope", CAPTURED_AT, 1000) is None


class TestHoldoutIntegrity:
    def test_holdout_recoveries_are_organic_by_construction(
        self, conn, merchant, customer, obligation
    ):
        cid = case_id()
        conn.execute(
            "INSERT INTO recovery_case (id, obligation_id, merchant_id, customer_id, state, arm) "
            "VALUES (%s, %s, %s, %s, 'awaiting_outcome', 'holdout')",
            (cid, obligation, merchant, customer),
        )
        attr = attribute_capture(conn, obligation, CAPTURED_AT, 250_000)
        assert attr.is_organic

    def test_contaminated_holdout_fails_loudly(self, conn, merchant, customer, obligation):
        # If the dispatcher ever acts on a holdout case, the denominator is
        # corrupt and every lift number computed from it is wrong. That must
        # crash, not degrade quietly.
        cid = case_id()
        conn.execute(
            "INSERT INTO recovery_case (id, obligation_id, merchant_id, customer_id, state, arm) "
            "VALUES (%s, %s, %s, %s, 'awaiting_outcome', 'holdout')",
            (cid, obligation, merchant, customer),
        )
        add_action(conn, cid, CAPTURED_AT - timedelta(hours=2))
        with pytest.raises(AssertionError, match="holdout is contaminated"):
            attribute_capture(conn, obligation, CAPTURED_AT, 250_000)


class TestIdempotentRecording:
    def test_outcome_recorded_once(self, conn, obligation, case):
        attr = attribute_capture(conn, obligation, CAPTURED_AT, 250_000)
        first = record_outcome(conn, attr)
        second = record_outcome(conn, attr)

        assert first is not None
        # A redelivered capture surviving the earlier dedup layers must not
        # double-count recovered revenue — that inflates the headline metric.
        assert second is None
        n = conn.execute(
            "SELECT count(*) AS n FROM recovery_outcome WHERE case_id = %s", (case,)
        ).fetchone()["n"]
        assert n == 1
