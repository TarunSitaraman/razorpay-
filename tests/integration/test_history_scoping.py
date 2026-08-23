"""`make history` must not destroy cases it did not create.

The original implementation opened with a TRUNCATE of every recovery table. That
looks harmless — the exploration history is regenerated deterministically, so
wiping it is fine — but the consumer's planning cases live in the same table and
went with it, and they could not be rebuilt: `processed_event` still recorded
their events as handled, so re-running the consumer reported 345,413 duplicates
and opened nothing.

The dedup layer was right. The events HAD been processed. The bug is that one
command's cleanup reached into another's state, and the failure mode was a
planning window that silently ceased to exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yukti.domain.ids import (
    action_id,
    case_id,
    customer_id,
    decision_id,
    new_id,
    obligation_id,
)

AT = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def customer(conn, merchant):
    return None


def _case_with_obligation(conn, merchant, state: str):
    cust = customer_id()
    conn.execute(
        "INSERT INTO customer (id, merchant_id, ltv_band) VALUES (%s,%s,'mid')",
        (cust, merchant))
    oid = obligation_id()
    conn.execute(
        "INSERT INTO obligation (id, merchant_id, customer_id, kind, amount_paise, "
        "due_at, state, version) VALUES (%s,%s,%s,'invoice',250000,%s,'open',1)",
        (oid, merchant, cust, AT))
    cid = case_id()
    conn.execute(
        "INSERT INTO recovery_case (id, obligation_id, merchant_id, customer_id, "
        "state, arm, opened_at) VALUES (%s,%s,%s,%s,%s,'treatment',%s)",
        (cid, oid, merchant, cust, state, AT - timedelta(days=1)))
    return cid


def test_a_planning_case_has_no_outcome_and_an_exploration_case_does(conn, merchant):
    """The discriminator the cleanup relies on, asserted directly.

    If this ever stops being true — if something starts writing an outcome at
    case creation — the scoped delete silently becomes a TRUNCATE again.
    """
    planning = _case_with_obligation(conn, merchant, "open")
    exploration = _case_with_obligation(conn, merchant, "awaiting_outcome")
    conn.execute(
        "INSERT INTO recovery_outcome (id, case_id, outcome, recovered_paise, "
        "attribution_window_h) VALUES (%s,%s,'recovered',250000,72)",
        (new_id("out"), exploration))

    have_outcome = {
        r["case_id"] for r in conn.execute(
            "SELECT case_id FROM recovery_outcome WHERE case_id = ANY(%s)",
            ([planning, exploration],))
    }
    assert have_outcome == {exploration}


def test_the_scoped_delete_spares_planning_cases(conn, merchant):
    """Run the exact cleanup `history_cli` performs and check what survives."""
    planning = _case_with_obligation(conn, merchant, "open")
    exploration = _case_with_obligation(conn, merchant, "awaiting_outcome")

    did = decision_id()
    conn.execute(
        "INSERT INTO agent_decision (id, case_id, trace_id, action_kind, channel, "
        "reason, policy_verdict) VALUES (%s,%s,%s,'message','sms','x','allow')",
        (did, exploration, new_id("yk")))
    conn.execute(
        "INSERT INTO recovery_action (id, decision_id, case_id, kind, channel, "
        "idempotency_key, status, cost_paise) "
        "VALUES (%s,%s,%s,'message','sms',%s,'dispatched',25)",
        (action_id(), did, exploration, new_id("idem")))
    conn.execute(
        "INSERT INTO recovery_outcome (id, case_id, outcome, recovered_paise, "
        "attribution_window_h) VALUES (%s,%s,'recovered',250000,72)",
        (new_id("out"), exploration))

    conn.execute("""
        CREATE TEMP TABLE _prior ON COMMIT DROP AS
        SELECT c.id FROM recovery_case c
         WHERE EXISTS (SELECT 1 FROM recovery_outcome o WHERE o.case_id = c.id)
    """)
    conn.execute("DELETE FROM recovery_outcome WHERE case_id IN (SELECT id FROM _prior)")
    conn.execute("DELETE FROM recovery_action  WHERE case_id IN (SELECT id FROM _prior)")
    conn.execute("DELETE FROM agent_decision   WHERE case_id IN (SELECT id FROM _prior)")
    conn.execute("DELETE FROM recovery_case    WHERE id IN (SELECT id FROM _prior)")

    survivors = {
        r["id"] for r in conn.execute(
            "SELECT id FROM recovery_case WHERE merchant_id = %s", (merchant,))
    }
    assert planning in survivors, "a planning case was destroyed by history cleanup"
    assert exploration not in survivors, "the exploration case was not cleaned up"


def test_history_cli_contains_no_truncate(conn):
    """A grep-level guard.

    Cheap, and it fails the moment someone reaches for the blunt instrument
    again. The scoped delete is a few lines longer and the reason is not
    obvious from reading it, which is exactly when a well-meaning tidy-up
    reintroduces the bug.
    """
    import pathlib

    import yukti_datagen.history_cli as mod

    source = pathlib.Path(mod.__file__).read_text()

    # Comments are stripped first. The module explains at length why it must
    # NOT truncate, and a naive grep flags that explanation — which would make
    # the guard fire on the documentation of the bug rather than the bug, and
    # the natural fix would be to delete the explanation.
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "TRUNCATE" not in code.upper(), (
        "history_cli truncates again — it would take the consumer's planning "
        "cases with it, and processed_event makes them unrecoverable"
    )
