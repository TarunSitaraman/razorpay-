"""Read models for the merchant console.

Deliberately plain SQL. These power a live dashboard during a demo, so the cost
of each query is something I want visible in the file rather than hidden behind
an ORM that might emit a join I did not intend.
"""

from __future__ import annotations

from typing import Any

import psycopg

LIVE_STATES = ("open", "planning", "scheduled", "acting", "awaiting_outcome")


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
