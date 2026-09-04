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
    conn: psycopg.Connection, merchant_id: str | None = None, limit: int = 50,
    state: str | None = None, arm: str | None = None,
    stop_reason: str | None = None,
) -> list[dict]:
    """Cases newest-first, narrowable by the three facets the console offers.

    `stop_reason` accepts the sentinel `stopped` for "any named reason", which
    is the question a merchant actually asks — they want the cases we walked
    away from, not one specific rule at a time.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if merchant_id:
        clauses.append("c.merchant_id = %s")
        params.append(merchant_id)
    if state:
        clauses.append("c.state = %s")
        params.append(state)
    if arm:
        clauses.append("c.arm = %s")
        params.append(arm)
    if stop_reason == "stopped":
        clauses.append("c.stop_reason IS NOT NULL")
    elif stop_reason:
        clauses.append("c.stop_reason = %s")
        params.append(stop_reason)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
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


# ---------------------------------------------------------------------------
# The case file
#
# Deliberately eight keyed lookups rather than one join. Attempts, decisions,
# rule evaluations, actions and audit rows all fan out from the same case, so a
# single statement returns their cartesian product and the caller has to
# de-duplicate it back into the shape it started as. Every lookup below is a
# primary key or an existing index, which is why the whole dossier costs less
# than the list view that links to it.
# ---------------------------------------------------------------------------

def case_spine(conn: psycopg.Connection, case_id: str) -> dict | None:
    """The case, its obligation, its customer and the merchant it belongs to."""
    row = conn.execute(
        """
        SELECT c.id, c.state, c.arm, c.stop_reason, c.opened_at, c.experiment_id,
               o.id            AS obligation_id,
               o.kind          AS obligation_kind,
               o.amount_paise, o.due_at,
               o.state         AS obligation_state,
               cu.id           AS customer_id,
               cu.consent, cu.opted_out_at, cu.ltv_band, cu.tenure_days,
               cu.preferred_channel, cu.archetype,
               cu.prior_payments, cu.prior_failures, cu.prior_contacts,
               cu.prior_contact_responses, cu.prior_optouts,
               cu.days_since_last_payment,
               cu.prior_unprompted_payments, cu.prior_prompted_payments,
               m.id            AS merchant_id,
               m.name          AS merchant_name,
               m.segment, m.mdr_bps
          FROM recovery_case c
          JOIN obligation o  ON o.id  = c.obligation_id
          JOIN customer   cu ON cu.id = c.customer_id
          JOIN merchant   m  ON m.id  = c.merchant_id
         WHERE c.id = %s
        """,
        (case_id,),
    ).fetchone()
    return dict(row) if row else None


def case_attempts(conn: psycopg.Connection, obligation_id: str) -> list[dict]:
    """Every attempt on the money, ours and the merchant's alike.

    `caused_by_action_id` is what separates a retry this system scheduled from
    one the merchant's own billing made — without it, organic recovery reads as
    something we caused, which is the error the whole product exists to correct.
    """
    rows = conn.execute(
        """
        SELECT id, rail, issuer, psp, status, decline_code, decline_text,
               amount_paise, attempted_at, caused_by_action_id
          FROM payment_attempt
         WHERE obligation_id = %s
         ORDER BY attempted_at
        """,
        (obligation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def case_collisions(
    conn: psycopg.Connection, customer_id: str, obligation_id: str
) -> list[dict]:
    """The same customer's other open money, on every other surface.

    This is the arbitration signal a per-surface tool cannot see: three agents
    each acting correctly on their own obligation still add up to three messages
    to one person in one afternoon.
    """
    rows = conn.execute(
        """
        SELECT o.id, o.kind, o.amount_paise, o.due_at,
               c.id AS case_id, c.state AS case_state
          FROM obligation o
          LEFT JOIN recovery_case c ON c.obligation_id = o.id
         WHERE o.customer_id = %s
           AND o.id <> %s
           AND o.state = 'open'
         ORDER BY o.amount_paise DESC
         LIMIT 20
        """,
        (customer_id, obligation_id),
    ).fetchall()
    return [dict(r) for r in rows]


def case_decisions(conn: psycopg.Connection, case_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, run_id, trace_id, action_kind, channel, scheduled_for, reason,
               confidence, expected_incr_margin_paise, alternatives_rejected,
               policy_verdict, risk, created_at
          FROM agent_decision
         WHERE case_id = %s
         ORDER BY created_at, id
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def case_policy_evaluations(
    conn: psycopg.Connection, decision_ids: list[str]
) -> list[dict]:
    """Every rule outcome for these decisions — the passes included.

    `policy_breakdown` filters to `verdict <> 'allow'` because an aggregate of
    rules that did nothing is noise. Here the opposite holds: "this check ran
    and passed" is what makes the trail evidence rather than an anecdote, and a
    reader who only ever sees blocks cannot tell whether the rest ran at all.
    """
    if not decision_ids:
        return []
    rows = conn.execute(
        """
        SELECT decision_id, pack, rule_id, verdict, reason
          FROM policy_evaluation
         WHERE decision_id = ANY(%s)
         ORDER BY decision_id, id
        """,
        (decision_ids,),
    ).fetchall()
    return [dict(r) for r in rows]


def case_actions(conn: psycopg.Connection, case_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, decision_id, kind, channel, idempotency_key, scheduled_for,
               dispatched_at, status, cost_paise, discount_paise, payload
          FROM recovery_action
         WHERE case_id = %s
         ORDER BY created_at
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def case_outcomes(conn: psycopg.Connection, case_id: str) -> list[dict]:
    """What happened. `action_id IS NULL` means it happened on its own."""
    rows = conn.execute(
        """
        SELECT id, action_id, outcome, recovered_paise, attribution_window_h,
               attributed_at
          FROM recovery_outcome
         WHERE case_id = %s
         ORDER BY attributed_at
        """,
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def case_audit(
    conn: psycopg.Connection, merchant_id: str, subject_ids: list[str],
    trace_ids: list[str],
) -> list[dict]:
    """The hash-chained rows that mention this case.

    Scoped by `merchant_id` first because that is the indexed column; filtering
    on `subject_id` alone is a sequential scan of the whole chain.
    """
    rows = conn.execute(
        """
        SELECT id, trace_id, actor, action, subject_id, detail,
               prev_hash, hash, created_at
          FROM audit_event
         WHERE merchant_id = %s
           AND (subject_id = ANY(%s) OR trace_id = ANY(%s))
         ORDER BY id
        """,
        (merchant_id, subject_ids, trace_ids),
    ).fetchall()
    return [dict(r) for r in rows]


def case_promises(conn: psycopg.Connection, obligation_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, promised_amount_paise, promised_for, source, state,
               confidence, created_at
          FROM promise_to_pay
         WHERE obligation_id = %s
         ORDER BY created_at
        """,
        (obligation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def planning_cycles(
    conn: psycopg.Connection, merchant_id: str | None = None, limit: int = 50
) -> list[dict]:
    """One row per completed planning cycle, newest first.

    A cycle is not a table. `plan_cycle` writes its result into the audit chain
    as a `plan_cycle.completed` event, and that is deliberately the only record:
    a separate summary table would be a second copy of numbers the chain already
    attests to, free to disagree with it. So the console reads the chain.

    The `as_of` window lives on the matching `.started` event, joined on
    `subject_id` (the run id) rather than on `trace_id`, because a trace can
    span a retry and the run id cannot.
    """
    where = "WHERE c.action = 'plan_cycle.completed'"
    params: list[Any] = []
    if merchant_id:
        where += " AND c.merchant_id = %s"
        params.append(merchant_id)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT c.subject_id                       AS id,
               c.merchant_id,
               c.created_at,
               s.detail ->> 'as_of'               AS as_of,
               c.detail                           AS detail
          FROM audit_event c
          LEFT JOIN audit_event s
                 ON s.subject_id = c.subject_id
                AND s.action = 'plan_cycle.started'
          {where}
         ORDER BY c.id DESC
         LIMIT %s
        """,
        params,
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        d = r["detail"] or {}
        out.append({
            "id": r["id"],
            "merchant_id": r["merchant_id"],
            "as_of": r["as_of"] or (r["created_at"].isoformat() if r["created_at"] else None),
            "considered": d.get("considered"),
            "stopped": d.get("stopped"),
            "dispatched": d.get("dispatched"),
            "escalated": d.get("escalated"),
            "suppressed": d.get("suppressed"),
            "contacts": d.get("contacts"),
            "discount_paise": d.get("discount_paise"),
            "agent_filtered": d.get("agent_filtered"),
            "agent_degraded": d.get("agent_degraded"),
            "lambda_contact": d.get("lambda_contact"),
            "lambda_discount": d.get("lambda_discount"),
            "dual_bound_paise": d.get("dual_bound_paise"),
            "planned_margin_paise": d.get("planned_margin_paise"),
            "candidates": d.get("candidates"),
            "optimality_ratio": d.get("optimality_ratio"),
        })
    return out


def audit_tip(conn: psycopg.Connection, merchant_id: str | None = None,
              limit: int = 20) -> list[dict]:
    """The newest rows of the chain, for showing that it is a chain.

    Returned newest-first for display, which is the reverse of the order the
    hashes link in — so the console labels the direction rather than leaving a
    reader to infer it from two truncated digests.
    """
    where = ""
    params: list[Any] = []
    if merchant_id:
        where = "WHERE merchant_id = %s"
        params.append(merchant_id)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT id, merchant_id, trace_id, actor, action, subject_id,
               prev_hash, hash, created_at
          FROM audit_event
          {where}
         ORDER BY id DESC
         LIMIT %s
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def case_facets(conn: psycopg.Connection, merchant_id: str | None = None) -> dict:
    """The values the case filters can actually take, for this book.

    Read from the data rather than hardcoded from the enums: offering a merchant
    a filter for a state none of their cases is in is a control that can only
    ever return nothing.
    """
    out: dict[str, list[dict]] = {}
    for column in ("state", "arm", "stop_reason"):
        clauses = [f"{column} IS NOT NULL"]
        params: list[Any] = []
        if merchant_id:
            clauses.append("merchant_id = %s")
            params.append(merchant_id)
        rows = conn.execute(
            f"SELECT {column} AS value, count(*) AS n "
            f"  FROM recovery_case WHERE {' AND '.join(clauses)} "
            f" GROUP BY {column} ORDER BY count(*) DESC",
            params,
        ).fetchall()
        out[column] = [{"value": r["value"], "n": r["n"]} for r in rows]
    return out


def approval_queue(
    conn: psycopg.Connection, merchant_id: str | None = None, limit: int = 100
) -> list[dict]:
    """Cases a rule held for a human, with the action that is waiting on them.

    The proposal is read from `agent_decision` rather than recomputed. A queue
    that re-planned on open would show the reviewer a different action from the
    one the escalation was raised about, and their approval would then attach to
    something nobody escalated.
    """
    clauses = ["c.state = 'escalated'", "d.policy_verdict = 'escalate'"]
    params: list[Any] = []
    if merchant_id:
        clauses.append("c.merchant_id = %s")
        params.append(merchant_id)
    params.append(limit)

    rows = conn.execute(
        f"""
        SELECT c.id                AS case_id,
               c.merchant_id, m.name AS merchant_name,
               c.opened_at,
               o.id                AS obligation_id,
               o.kind              AS obligation_kind,
               o.amount_paise,
               cu.id               AS customer_id,
               cu.ltv_band, cu.consent,
               d.id                AS decision_id,
               d.action_kind, d.channel, d.scheduled_for, d.reason,
               d.expected_incr_margin_paise,
               d.alternatives_rejected,
               d.created_at        AS decided_at,
               a.decline_code, a.rail, a.issuer
          FROM recovery_case c
          JOIN merchant    m  ON m.id = c.merchant_id
          JOIN obligation  o  ON o.id = c.obligation_id
          JOIN customer    cu ON cu.id = o.customer_id
          -- The escalating decision is the newest one on the case that carries
          -- the escalate verdict; a case re-planned later may have others.
          JOIN LATERAL (
              SELECT * FROM agent_decision
               WHERE case_id = c.id AND policy_verdict = 'escalate'
               ORDER BY created_at DESC LIMIT 1
          ) d ON true
          LEFT JOIN LATERAL (
              SELECT decline_code, rail, issuer
                FROM payment_attempt
               WHERE obligation_id = o.id AND status = 'failed'
               ORDER BY attempted_at DESC LIMIT 1
          ) a ON true
         WHERE {' AND '.join(clauses)}
         ORDER BY o.amount_paise DESC
         LIMIT %s
        """,
        params,
    ).fetchall()

    out = [dict(r) for r in rows]
    if not out:
        return out

    # The rules that escalated each decision, so the reviewer is told what they
    # are being asked to override rather than merely that something objected.
    ev = conn.execute(
        """
        SELECT decision_id, rule_id, verdict, reason
          FROM policy_evaluation
         WHERE decision_id = ANY(%s) AND verdict <> 'allow'
         ORDER BY decision_id, id
        """,
        ([r["decision_id"] for r in out],),
    ).fetchall()
    by_decision: dict[str, list[dict]] = {}
    for e in ev:
        by_decision.setdefault(e["decision_id"], []).append(dict(e))
    for r in out:
        r["rules"] = by_decision.get(r["decision_id"], [])
    return out
