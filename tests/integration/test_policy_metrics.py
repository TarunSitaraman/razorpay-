"""The guardrail panel has to be able to name the rule that bit.

`policy_evaluation` records the verdicts for the action that was finally taken,
and that action passed every rule by construction — so the regulatory pack is
stored as an unbroken wall of `allow`. The refusals live on the decision, in
`alternatives_rejected`: the console was therefore able to show a merchant every
regulation that passed and none that stopped anything.

These tests pin the recovery of those refusals, and the one property that makes
the number quotable: a rule that refuses two candidate actions on one obligation
is one blocked case, not two.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from yukti.api import queries
from yukti.domain.ids import (
    case_id,
    customer_id,
    decision_id,
    new_id,
    obligation_id,
    trace_id,
)

AT = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


def _decision(conn, merchant, *, amount_paise: int, alternatives: list[dict],
              run: bool = True) -> str:
    cust = customer_id()
    conn.execute(
        "INSERT INTO customer (id, merchant_id, ltv_band) VALUES (%s,%s,'mid')",
        (cust, merchant))
    oid = obligation_id()
    conn.execute(
        "INSERT INTO obligation (id, merchant_id, customer_id, kind, amount_paise, "
        "due_at, state, version) VALUES (%s,%s,%s,'invoice',%s,%s,'open',1)",
        (oid, merchant, cust, amount_paise, AT))
    cid = case_id()
    conn.execute(
        "INSERT INTO recovery_case (id, obligation_id, merchant_id, customer_id, "
        "state, arm, opened_at) VALUES (%s,%s,%s,%s,'scheduled','treatment',%s)",
        (cid, oid, merchant, cust, AT - timedelta(days=1)))
    rid = None
    if run:
        rid = new_id("run")
        conn.execute(
            "INSERT INTO agent_run (id, merchant_id, kind, trace_id, status) "
            "VALUES (%s,%s,'planner',%s,'completed')", (rid, merchant, trace_id()))
    did = decision_id()
    conn.execute(
        "INSERT INTO agent_decision (id, run_id, case_id, trace_id, action_kind, "
        "channel, reason, policy_verdict, alternatives_rejected) "
        "VALUES (%s,%s,%s,%s,'voice_call','voice','x','allow',%s)",
        (did, rid, cid, trace_id(), json.dumps(alternatives)))
    return did


def _afa(action: str) -> dict:
    return {"action": action, "channel": "none", "rejected_by": "POLICY",
            "blocked_by": "RBI_AFA_LIMIT", "reason": "above the AFA-free limit"}


def test_a_regulatory_refusal_is_reported_by_rule(conn, merchant):
    _decision(conn, merchant, amount_paise=4_000_000,
              alternatives=[_afa("silent_retry")])

    rows = {r["rule_id"]: r for r in queries.refused_alternatives(conn, merchant)}

    assert rows["RBI_AFA_LIMIT"]["n"] == 1
    assert rows["RBI_AFA_LIMIT"]["amount_paise"] == 4_000_000


def test_two_actions_refused_on_one_obligation_count_once(conn, merchant):
    """Otherwise the panel doubles the money, and the demo quotes it."""
    _decision(conn, merchant, amount_paise=4_000_000,
              alternatives=[_afa("silent_retry"), _afa("schedule_debit")])

    rows = {r["rule_id"]: r for r in queries.refused_alternatives(conn, merchant)}

    assert rows["RBI_AFA_LIMIT"]["n"] == 1
    assert rows["RBI_AFA_LIMIT"]["amount_paise"] == 4_000_000


def test_an_allocator_rejection_is_not_a_policy_block(conn, merchant):
    """"We could not afford this" and "we were not allowed to" are different
    sentences, and the guardrail panel is only allowed to make the second."""
    _decision(conn, merchant, amount_paise=4_000_000, alternatives=[
        {"action": "message", "channel": "sms", "rejected_by": "ALLOCATOR",
         "reason": "expected margin 1200 paise"},
    ])

    assert queries.refused_alternatives(conn, merchant) == []


def test_exploration_decisions_are_excluded(conn, merchant):
    """The randomised training history has a NULL run_id and is not a decision
    the merchant made; the feed already excludes it, and so must the counts."""
    _decision(conn, merchant, amount_paise=4_000_000,
              alternatives=[_afa("silent_retry")], run=False)

    assert queries.refused_alternatives(conn, merchant) == []


def test_the_merchant_filter_scopes_the_counts(conn, merchant):
    _decision(conn, merchant, amount_paise=4_000_000,
              alternatives=[_afa("silent_retry")])

    other = queries.refused_alternatives(conn, "mrc_does_not_exist")

    assert other == []
