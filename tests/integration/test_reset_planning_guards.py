"""`reset-planning` must never touch the exploration history.

This exists because the first version of that command did. It reopened 2,156
exploration cases and deleted their `agent_decision` and `recovery_action` rows
while leaving `recovery_outcome` intact — which would have made every one of
them read as a CONTROL row that recovered. The RCT would have been silently
biased rather than visibly broken, and the only symptom would have been an
uplift model that quietly got worse.

Planning output and exploration history live in the same tables, so the
separation is a property of the queries and nothing else enforces it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from yukti.cli import app
from yukti.domain.ids import (
    action_id,
    case_id,
    customer_id,
    decision_id,
    new_id,
    obligation_id,
    run_id,
    trace_id,
)

AT = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


def _case(conn, merchant, state="open", stop_reason=None):
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
        "state, stop_reason, arm, opened_at) VALUES (%s,%s,%s,%s,%s,%s,'treatment',%s)",
        (cid, oid, merchant, cust, state, stop_reason, AT - timedelta(days=1)))
    return cid


@pytest.fixture
def customer(conn, merchant):
    """Override: these tests mint their own customers per case."""
    return None


def _exploration_case(conn, merchant):
    """An exploration case: decision with NO run_id, an action, and an outcome."""
    cid = _case(conn, merchant, state="awaiting_outcome")
    did = decision_id()
    conn.execute(
        "INSERT INTO agent_decision (id, case_id, trace_id, action_kind, channel, "
        "reason, policy_verdict) VALUES (%s,%s,%s,'message','whatsapp',"
        "'randomised exploration','allow')", (did, cid, trace_id()))
    conn.execute(
        "INSERT INTO recovery_action (id, decision_id, case_id, kind, channel, "
        "idempotency_key, status, cost_paise) "
        "VALUES (%s,%s,%s,'message','whatsapp',%s,'dispatched',75)",
        (action_id(), did, cid, new_id("idem")))
    conn.execute(
        "INSERT INTO recovery_outcome (id, case_id, outcome, recovered_paise, "
        "attribution_window_h) VALUES (%s,%s,'recovered',250000,72)",
        (new_id("out"), cid))
    return cid, did


def _planned_case(conn, merchant):
    """A planning case: decision attached to a planner run, no outcome."""
    # stop_reason set in the same INSERT: the schema's stop_reason_iff_stopped
    # constraint refuses a stopped case with no named rule, which is the
    # invariant the whole stopping-rules design rests on.
    cid = _case(conn, merchant, state="stopped", stop_reason="lost_cause")
    rid = run_id()
    conn.execute(
        "INSERT INTO agent_run (id, merchant_id, kind, trace_id, status) "
        "VALUES (%s,%s,'planner',%s,'completed')", (rid, merchant, trace_id()))
    did = decision_id()
    conn.execute(
        "INSERT INTO agent_decision (id, run_id, case_id, trace_id, action_kind, "
        "channel, reason, policy_verdict) "
        "VALUES (%s,%s,%s,%s,'suppress','none','stopped','allow')",
        (did, rid, cid, trace_id()))
    return cid, did


def _reset(merchant: str):
    result = CliRunner().invoke(
        app, ["reset-planning", "--merchant", merchant, "--yes"])
    assert result.exit_code == 0, result.output
    return result


def test_exploration_rows_survive_a_reset(conn, merchant):
    explored, explored_decision = _exploration_case(conn, merchant)
    conn.commit()

    _reset(merchant)

    row = conn.execute("SELECT state FROM recovery_case WHERE id = %s",
                       (explored,)).fetchone()
    assert row["state"] == "awaiting_outcome", "an exploration case was reopened"

    assert conn.execute(
        "SELECT count(*) AS n FROM agent_decision WHERE id = %s", (explored_decision,)
    ).fetchone()["n"] == 1, "an exploration decision was deleted"

    assert conn.execute(
        "SELECT count(*) AS n FROM recovery_action WHERE case_id = %s", (explored,)
    ).fetchone()["n"] == 1, "an exploration action was deleted — the treatment " \
                            "assignment that identifies uplift"


def test_the_treatment_flag_survives(conn, merchant):
    """The specific corruption: treated rows becoming control rows.

    `features.build_frame` derives `treated` from whether a recovery_action
    exists. Deleting the action while keeping the outcome does not remove the
    row from training — it moves it to the other arm.
    """
    explored, _ = _exploration_case(conn, merchant)
    conn.commit()

    _reset(merchant)

    treated = conn.execute(
        """
        SELECT (act.id IS NOT NULL) AS treated
          FROM recovery_case c
          LEFT JOIN agent_decision d   ON d.case_id = c.id
          LEFT JOIN recovery_action act ON act.decision_id = d.id
         WHERE c.id = %s
        """,
        (explored,),
    ).fetchone()["treated"]
    assert treated is True, "a treated exploration row would now train as control"


def test_planning_rows_are_reset(conn, merchant):
    planned, planned_decision = _planned_case(conn, merchant)
    conn.commit()

    _reset(merchant)

    row = conn.execute("SELECT state, stop_reason FROM recovery_case WHERE id = %s",
                       (planned,)).fetchone()
    assert row["state"] == "open"
    assert row["stop_reason"] is None

    assert conn.execute(
        "SELECT count(*) AS n FROM agent_decision WHERE id = %s", (planned_decision,)
    ).fetchone()["n"] == 0


def test_a_mixed_merchant_resets_only_the_planning_half(conn, merchant):
    """The realistic case, and the one the bug actually hit."""
    explored, _ = _exploration_case(conn, merchant)
    planned, _ = _planned_case(conn, merchant)
    conn.commit()

    _reset(merchant)

    rows = conn.execute(
        "SELECT id, state FROM recovery_case WHERE id = ANY(%s)",
        ([explored, planned],),
    ).fetchall()
    states = {r["id"]: r["state"] for r in rows}
    assert states[explored] == "awaiting_outcome"
    assert states[planned] == "open"


def test_budget_consumption_is_cleared(conn, merchant):
    conn.execute(
        "INSERT INTO budget_ledger (merchant_id, kind, window_start, limit_val, "
        "consumed_val) VALUES (%s,'contact',%s,50,30)", (merchant, AT.date()))
    conn.commit()

    _reset(merchant)

    assert conn.execute(
        "SELECT consumed_val FROM budget_ledger WHERE merchant_id = %s", (merchant,)
    ).fetchone()["consumed_val"] == 0
