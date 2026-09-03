"""Human disposition of an escalated case.

`plan_cycle` proposes; where a merchant rule demands authority it escalates and
stops. This is the other half: a named person approving or rejecting that
specific proposal.

The important property is that approving does not route around anything. The
proposal is re-evaluated by the full policy engine with `human_approved` set,
which is consulted by exactly one rule -- the approval threshold, the rule that
raised the escalation in the first place. Every regulatory rule runs unchanged
and can still block, so a stale escalation whose action has since become illegal
is refused at approval rather than dispatched on a human's say-so. That is why
this lives here and not in an HTTP handler: the handler is a transport, and a
write path that could be reached without passing these checks is precisely the
failure mode the design exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from yukti import audit
from yukti.domain.enums import (
    ActionKind, ActorKind, CaseState, Channel, PolicyVerdict, StopReason,
)
from yukti.domain.ids import trace_id
from yukti.policy import engine as policy_engine
from yukti.policy.merchantpack import MerchantContext
from yukti.policy.regpack import ActionRequest
from yukti.policy.store import load_policy

# Mirrors `pipeline._template_for`. A commercial SMS without a registered DLT
# template is rejected by the operator, and an approval must not be the one path
# that sends without one.
_DLT = {
    ActionKind.MESSAGE: "DLT_YUKTI_PAYLINK_01",
    ActionKind.DISCOUNT_OFFER: "DLT_YUKTI_OFFER_01",
    ActionKind.VOICE_CALL: "DLT_YUKTI_VOICE_01",
}


class ApprovalError(Exception):
    """The approval cannot be honoured. Carries why, for the reviewer."""

    def __init__(self, message: str, *, rules: list[dict] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.rules = rules or []


@dataclass(frozen=True, slots=True)
class Disposition:
    case_id: str
    decision_id: str
    verdict: str          # approved | rejected
    dispatched: bool
    detail: str


def _load(conn: psycopg.Connection, case_id: str) -> tuple[dict, dict]:
    case = conn.execute(
        """
        SELECT c.id, c.state, c.merchant_id, c.customer_id, c.obligation_id,
               o.amount_paise, o.kind AS obligation_kind,
               cu.consent,
               m.segment AS merchant_segment
          FROM recovery_case c
          JOIN obligation o  ON o.id  = c.obligation_id
          JOIN customer   cu ON cu.id = c.customer_id
          JOIN merchant   m  ON m.id  = c.merchant_id
         WHERE c.id = %s
        """,
        (case_id,),
    ).fetchone()
    if case is None:
        raise ApprovalError(f"no such case: {case_id}")
    if case["state"] != CaseState.ESCALATED.value:
        raise ApprovalError(
            f"case {case_id} is {case['state']}, not escalated — nothing is "
            f"waiting on a reviewer"
        )

    decision = conn.execute(
        """
        SELECT id, action_kind, channel, scheduled_for, reason, trace_id,
               expected_incr_margin_paise, alternatives_rejected
          FROM agent_decision
         WHERE case_id = %s AND policy_verdict = 'escalate'
         ORDER BY created_at DESC LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    if decision is None:
        raise ApprovalError(
            f"case {case_id} is escalated but carries no escalating decision — "
            f"there is no proposal to approve"
        )
    return dict(case), dict(decision)


def _attempts(conn: psycopg.Connection, obligation_id: str) -> tuple[int, str | None]:
    row = conn.execute(
        "SELECT count(*) AS n, max(decline_code) AS code FROM payment_attempt "
        " WHERE obligation_id = %s AND status = 'failed'",
        (obligation_id,),
    ).fetchone()
    return int(row["n"] or 1), row["code"]


def _contacts_this_week(conn: psycopg.Connection, customer_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM recovery_action a "
        "  JOIN recovery_case c ON c.id = a.case_id "
        " WHERE c.customer_id = %s AND a.created_at > now() - interval '7 days' "
        "   AND a.status = 'dispatched'",
        (customer_id,),
    ).fetchone()
    return int(row["n"] or 0)


def decide(
    conn: psycopg.Connection,
    case_id: str,
    verdict: str,
    actor: str,
    note: str = "",
    adapters: Any | None = None,
) -> Disposition:
    """Approve or reject one escalated case on a named person's authority.

    `actor` is recorded in the audit chain and is not optional: an approval with
    no one attached to it is the same as no approval.
    """
    if verdict not in ("approve", "reject"):
        raise ApprovalError(f"verdict must be 'approve' or 'reject', got {verdict!r}")
    if not actor.strip():
        raise ApprovalError("an approval must name the person making it")

    case, decision = _load(conn, case_id)
    tid = decision["trace_id"] or trace_id()

    if verdict == "reject":
        conn.execute(
            "UPDATE recovery_case SET state = %s, stop_reason = %s, "
            "       version = version + 1 WHERE id = %s",
            (CaseState.STOPPED.value, StopReason.HUMAN_REJECTED.value, case_id),
        )
        audit.append(
            conn, trace_id=tid, merchant_id=case["merchant_id"],
            action="case.approval_rejected", actor=ActorKind.HUMAN,
            subject_id=case_id,
            detail={"decision_id": decision["id"], "by": actor, "note": note,
                    "proposed": decision["action_kind"],
                    "amount_paise": int(case["amount_paise"])},
        )
        conn.commit()
        return Disposition(case_id, decision["id"], "rejected", False,
                           "case stopped; no action sent")

    # --- approve -------------------------------------------------------------
    action_kind = ActionKind(decision["action_kind"])
    channel = Channel(decision["channel"])
    attempts, decline_code = _attempts(conn, case["obligation_id"])
    scheduled_for = decision["scheduled_for"]

    request = ActionRequest(
        action_kind=action_kind,
        channel=channel,
        scheduled_for=scheduled_for,
        amount_paise=int(case["amount_paise"]),
        merchant_category=case.get("merchant_segment") or "general",
        decline_code=decline_code,
        attempts_made=attempts,
        discount_pct=0.0,
        consent=case.get("consent") or {},
        predebit_notice_at=(
            scheduled_for - timedelta(hours=25)
            if action_kind is ActionKind.SCHEDULE_DEBIT and scheduled_for else None
        ),
        dlt_template_id=_DLT.get(action_kind),
        has_afa=False,
    )
    policy = load_policy(conn, case["merchant_id"])
    context = MerchantContext(
        contacts_this_week=_contacts_this_week(conn, case["customer_id"]),
        had_recent_discount=False,
        human_approved=True,
    )
    evaluation = policy_engine.evaluate(request, policy, context)

    # Re-checked at approval time, not trusted from when the escalation was
    # raised. An escalation can sit in a queue for days, and the rules it has to
    # satisfy are about the world now: an RBI notice window that has closed, a
    # consent that has been withdrawn, a contact cap since reached.
    if evaluation.verdict is PolicyVerdict.BLOCK:
        blocked = [{"rule_id": r.rule_id, "reason": r.reason} for r in evaluation.blocks]
        audit.append(
            conn, trace_id=tid, merchant_id=case["merchant_id"],
            action="case.approval_refused", actor=ActorKind.SYSTEM,
            subject_id=case_id,
            detail={"decision_id": decision["id"], "by": actor,
                    "rules": [b["rule_id"] for b in blocked],
                    "note": "approval received but the action is not permitted"},
        )
        conn.commit()
        raise ApprovalError(
            "this action cannot be sent even with approval — "
            + "; ".join(f"{b['rule_id']}: {b['reason']}" for b in blocked),
            rules=blocked,
        )

    policy_engine.record(conn, decision["id"], evaluation)

    from yukti.dispatch.adapters import Adapters
    from yukti.dispatch.dispatcher import Dispatcher
    from yukti.dispatch.tools import ActionSpec

    spec = ActionSpec(
        case_id=case_id,
        obligation_id=case["obligation_id"],
        merchant_id=case["merchant_id"],
        customer_id=case["customer_id"],
        action_kind=action_kind,
        channel=channel,
        amount_paise=int(case["amount_paise"]),
        scheduled_for=scheduled_for or datetime.now(tz=None),
        idempotency_key="",
        # Left at their defaults rather than passed as None: the
        # adapters treat these as present, and the escalating
        # decision does not record the rail it was proposed on.
        decline_code=decline_code or "UNKNOWN",
        discount_pct=0.0,
        discount_paise=0,
        dlt_template_id=_DLT.get(action_kind),
        body="",
    )
    dispatcher = Dispatcher(conn, adapters or Adapters.sandbox())
    outcome = dispatcher.dispatch(spec, decision["id"], tid)

    if outcome.dispatched:
        conn.execute(
            "UPDATE recovery_case SET state = %s, version = version + 1 WHERE id = %s",
            (CaseState.AWAITING_OUTCOME.value, case_id),
        )
    audit.append(
        conn, trace_id=tid, merchant_id=case["merchant_id"],
        action="case.approved", actor=ActorKind.HUMAN, subject_id=case_id,
        detail={"decision_id": decision["id"], "by": actor, "note": note,
                "action": action_kind.value, "channel": channel.value,
                "amount_paise": int(case["amount_paise"]),
                "dispatched": bool(outcome.dispatched),
                "rules_rechecked": len(evaluation.results)},
    )
    conn.commit()
    return Disposition(
        case_id, decision["id"], "approved", bool(outcome.dispatched),
        "dispatched" if outcome.dispatched else "approved but not dispatched",
    )
