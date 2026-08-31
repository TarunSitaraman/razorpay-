"""Read models for the merchant console.

Deliberately plain SQL. These power a live dashboard during a demo, so the cost
of each query is something I want visible in the file rather than hidden behind
an ORM that might emit a join I did not intend.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

# `held_out` counts as live: the case was deliberately not worked, but the
# obligation is still open and can still resolve on its own — which is the
# entire point of holding it out. Dropping it here would quietly shrink revenue
# at risk by the holdout share.
LIVE_STATES = ("open", "planning", "scheduled", "acting", "awaiting_outcome",
               "held_out")


def revenue_at_risk(conn: psycopg.Connection, merchant_id: str | None = None) -> dict[str, Any]:
    """Open recoverable money, split by surface.

    This is the number the console leads with, and the one a merchant checks
    against their own books, so it counts obligations rather than cases: a case
    is our unit of work, an obligation is their unit of money.
    """
    where = "WHERE o.state = 'open'"
    params: list[Any] = []
    if merchant_id:
        where += " AND o.merchant_id = %s"
        params.append(merchant_id)

    rows = conn.execute(
        f"""
        SELECT o.kind,
               count(*)                AS cases,
               coalesce(sum(o.amount_paise), 0)::bigint AS amount_paise
          FROM obligation o
          {where}
         GROUP BY o.kind
         ORDER BY amount_paise DESC
        """,
        params,
    ).fetchall()

    return {
        "total_paise": sum(r["amount_paise"] for r in rows),
        "total_cases": sum(r["cases"] for r in rows),
        "by_surface": [dict(r) for r in rows],
    }


def pipeline_counts(conn: psycopg.Connection, merchant_id: str | None = None) -> dict[str, int]:
    """Case counts by state — the live view of what the system is working on."""
    where = ""
    params: list[Any] = []
    if merchant_id:
        where = "WHERE merchant_id = %s"
        params.append(merchant_id)
    rows = conn.execute(
        f"SELECT state, count(*) AS n FROM recovery_case {where} GROUP BY state", params
    ).fetchall()
    return {r["state"]: r["n"] for r in rows}


def stop_reason_breakdown(conn: psycopg.Connection, merchant_id: str | None = None) -> list[dict]:
    """Why work stopped, grouped by named rule.

    The track brief asks for stopping rules explicitly, so the console shows
    them by name alongside the money each rule chose not to chase. "Money we
    deliberately did not spend" is a headline number here, not a footnote.
    """
    where = "WHERE c.state = 'stopped'"
    params: list[Any] = []
    if merchant_id:
        where += " AND c.merchant_id = %s"
        params.append(merchant_id)
    rows = conn.execute(
        f"""
        SELECT c.stop_reason,
               count(*) AS cases,
               coalesce(sum(o.amount_paise), 0)::bigint AS amount_paise
          FROM recovery_case c
          JOIN obligation o ON o.id = c.obligation_id
          {where}
         GROUP BY c.stop_reason
         ORDER BY cases DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def arm_split(conn: psycopg.Connection, merchant_id: str | None = None) -> list[dict]:
    """Holdout vs treatment, with recovery counts.

    The holdout column is what makes every other number on the dashboard
    interpretable: without it, "recovered" is gross and flatters the system.
    """
    where = ""
    params: list[Any] = []
    if merchant_id:
        where = "WHERE c.merchant_id = %s"
        params.append(merchant_id)
    rows = conn.execute(
        f"""
        SELECT c.arm,
               count(*) AS cases,
               count(*) FILTER (WHERE c.state = 'recovered') AS recovered,
               coalesce(sum(o.amount_paise) FILTER (WHERE c.state = 'recovered'), 0)::bigint
                   AS recovered_paise
          FROM recovery_case c
          JOIN obligation o ON o.id = c.obligation_id
          {where}
         GROUP BY c.arm
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def merchants(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT m.id, m.name, m.segment, m.mdr_bps,
               count(DISTINCT cu.id) AS customers
          FROM merchant m
          LEFT JOIN customer cu ON cu.merchant_id = m.id
         GROUP BY m.id, m.name, m.segment, m.mdr_bps
         ORDER BY m.name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def recent_cases(
    conn: psycopg.Connection, merchant_id: str | None = None, limit: int = 50
) -> list[dict]:
    where = ""
    params: list[Any] = []
    if merchant_id:
        where = "WHERE c.merchant_id = %s"
        params.append(merchant_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT c.id, c.state, c.arm, c.stop_reason, c.opened_at,
               o.kind, o.amount_paise,
               a.decline_code, a.rail, a.issuer
          FROM recovery_case c
          JOIN obligation o ON o.id = c.obligation_id
          LEFT JOIN LATERAL (
              SELECT decline_code, rail, issuer
                FROM payment_attempt
               WHERE obligation_id = o.id AND status = 'failed'
               ORDER BY attempted_at DESC
               LIMIT 1
          ) a ON true
          {where}
         ORDER BY c.opened_at DESC
         LIMIT %s
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def failure_mix(conn: psycopg.Connection, merchant_id: str | None = None) -> list[dict]:
    """Decline-code distribution over open obligations."""
    where = "WHERE a.status = 'failed' AND o.state = 'open'"
    params: list[Any] = []
    if merchant_id:
        where += " AND o.merchant_id = %s"
        params.append(merchant_id)
    rows = conn.execute(
        f"""
        SELECT a.decline_code,
               count(*) AS n,
               coalesce(sum(o.amount_paise), 0)::bigint AS amount_paise
          FROM payment_attempt a
          JOIN obligation o ON o.id = a.obligation_id
          {where}
         GROUP BY a.decline_code
         ORDER BY amount_paise DESC
         LIMIT 12
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def recent_decisions(
    conn: psycopg.Connection, merchant_id: str | None = None, limit: int = 50
) -> list[dict]:
    """The decision feed — what the planner chose, and what it turned down.

    `alternatives_rejected` is the column that makes this worth showing. Every
    other recovery product can tell a merchant what it did; this can tell them
    what it declined to do and which rule declined it, which is the difference
    between a log and an explanation.
    """
    where = ""
    params: list[Any] = []
    if merchant_id:
        where = "WHERE c.merchant_id = %s"
        params.append(merchant_id)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT d.id, d.case_id, d.trace_id, d.action_kind, d.channel,
               d.scheduled_for, d.reason, d.confidence,
               d.expected_incr_margin_paise, d.policy_verdict,
               d.alternatives_rejected, d.created_at,
               c.arm, c.state AS case_state, c.stop_reason,
               o.kind AS obligation_kind, o.amount_paise,
               act.status AS action_status, act.cost_paise, act.discount_paise
          FROM agent_decision d
          JOIN recovery_case c ON c.id = d.case_id
          JOIN obligation o    ON o.id = c.obligation_id
          LEFT JOIN recovery_action act ON act.decision_id = d.id
          -- Planning decisions only. The exploration history lives in the same
          -- table with a NULL run_id, and showing a merchant a randomised probe
          -- from the training period as if it were a live decision would be
          -- straightforwardly misleading.
         WHERE d.run_id IS NOT NULL
           {where.replace('WHERE', 'AND') if where else ''}
         -- Funded actions first, then suppressions, each newest-first. A cycle
         -- suppresses far more cases than it funds, and they are written last,
         -- so a plain chronological page was returning 25 suppressions and none
         -- of the actions -- the feed showed nothing the system actually did.
         ORDER BY (d.action_kind = 'suppress'), d.created_at DESC, d.id DESC
         LIMIT %s
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def policy_breakdown(
    conn: psycopg.Connection, merchant_id: str | None = None
) -> list[dict]:
    """Which rules fired, how often, and on how much money.

    Grouped by pack so the three kinds of "no" stay distinguishable: a
    regulatory block, a merchant limit, and a business stopping rule are
    different conversations with the merchant.
    """
    where = ""
    params: list[Any] = []
    if merchant_id:
        where = "AND c.merchant_id = %s"
        params.append(merchant_id)
    rows = conn.execute(
        f"""
        SELECT pe.pack, pe.rule_id, pe.verdict,
               count(*)                                  AS n,
               coalesce(sum(o.amount_paise), 0)::bigint   AS amount_paise
          FROM policy_evaluation pe
          JOIN agent_decision d ON d.id = pe.decision_id
          JOIN recovery_case c  ON c.id = d.case_id
          JOIN obligation o     ON o.id = c.obligation_id
         WHERE pe.verdict <> 'allow'
           {where}
         GROUP BY pe.pack, pe.rule_id, pe.verdict
         ORDER BY n DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def refused_alternatives(
    conn: psycopg.Connection, merchant_id: str | None = None
) -> list[dict]:
    """Actions a policy rule refused, recovered from the decisions themselves.

    `policy_evaluation` only records the rule outcomes for the action that was
    finally chosen, and by construction that action passed every rule — so the
    regulatory pack shows up there as a wall of `allow` and the console can
    never say *which* rule stopped *what*. The refusals are on the decision, in
    `alternatives_rejected`: "the allocator wanted a silent retry, RBI_AFA_LIMIT
    said no". That is the compliant-escalation claim, and it is the one thing
    the guardrail panel was missing.

    Counted per decision, not per rejected candidate: one obligation above the
    AFA ceiling refuses both `silent_retry` and `schedule_debit`, and reporting
    that as two blocked cases would double-count the money.
    """
    where = ""
    params: list[Any] = []
    if merchant_id:
        where = "AND c.merchant_id = %s"
        params.append(merchant_id)
    rows = conn.execute(
        f"""
        WITH refused AS (
            SELECT DISTINCT d.id AS decision_id,
                   alt->>'blocked_by' AS rule_id,
                   o.amount_paise
              FROM agent_decision d
              JOIN recovery_case c ON c.id = d.case_id
              JOIN obligation o    ON o.id = c.obligation_id
              CROSS JOIN LATERAL jsonb_array_elements(d.alternatives_rejected) AS alt
             WHERE d.run_id IS NOT NULL
               AND alt->>'rejected_by' = 'POLICY'
               AND alt->>'blocked_by' IS NOT NULL
               {where}
        )
        SELECT rule_id,
               count(*)                                 AS n,
               coalesce(sum(amount_paise), 0)::bigint   AS amount_paise
          FROM refused
         GROUP BY rule_id
         ORDER BY n DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def budget_state(
    conn: psycopg.Connection, merchant_id: str | None = None,
    window: date | None = None,
) -> list[dict]:
    """Authorised against consumed, for the draining-budget panel."""
    where = "WHERE b.window_start = %s"
    params: list[Any] = [window or date.today()]
    if merchant_id:
        where += " AND b.merchant_id = %s"
        params.append(merchant_id)
    rows = conn.execute(
        f"""
        SELECT b.merchant_id, m.name, b.kind, b.window_start,
               b.limit_val, b.consumed_val,
               greatest(0, b.limit_val - b.consumed_val) AS remaining
          FROM budget_ledger b
          JOIN merchant m ON m.id = b.merchant_id
          {where}
         ORDER BY m.name, b.kind
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def money_not_chased(
    conn: psycopg.Connection, merchant_id: str | None = None
) -> dict[str, Any]:
    """The headline the brief asks for, framed the way it should be read.

    Two different things are reported separately and must not be added together:
    money we STOPPED on (a named rule says this is not worth working) and money
    we merely did not FUND this cycle (the budget went further elsewhere, and the
    case is still open tomorrow). Summing them would overstate what the system
    walked away from.
    """
    where = ""
    params: list[Any] = []
    if merchant_id:
        where = "AND c.merchant_id = %s"
        params.append(merchant_id)

    stopped = conn.execute(
        f"""
        SELECT c.stop_reason,
               count(*)                                 AS cases,
               coalesce(sum(o.amount_paise), 0)::bigint  AS amount_paise
          FROM recovery_case c
          JOIN obligation o ON o.id = c.obligation_id
         WHERE c.state = 'stopped' {where}
         GROUP BY c.stop_reason
         ORDER BY amount_paise DESC
        """,
        params,
    ).fetchall()

    unfunded = conn.execute(
        f"""
        SELECT count(*)                                 AS cases,
               coalesce(sum(o.amount_paise), 0)::bigint  AS amount_paise
          FROM recovery_case c
          JOIN obligation o     ON o.id = c.obligation_id
          JOIN agent_decision d ON d.case_id = c.id AND d.run_id IS NOT NULL
         WHERE c.state = 'open' AND d.action_kind = 'suppress' {where}
        """,
        params,
    ).fetchone()

    return {
        "stopped_by_rule": [dict(r) for r in stopped],
        "stopped_total_paise": sum(r["amount_paise"] for r in stopped),
        "considered_not_funded_paise": int(unfunded["amount_paise"]),
        "considered_not_funded_cases": int(unfunded["cases"]),
    }
