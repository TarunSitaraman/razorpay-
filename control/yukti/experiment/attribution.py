"""Attribution: deciding whether Yukti caused a recovery, or merely witnessed it.

This is the measurement that every other claim depends on. Gross recovered
revenue is easy and meaningless — roughly a third of failed payments resolve on
their own, so a system that contacts everyone can claim credit for all of them.

Two mechanisms, and the distinction between them matters:

  * **Attribution** links a capture to a preceding action within that action's
    window. It answers "which action was in flight when this landed?" It does
    NOT answer "did the action cause it", and must never be presented as if it
    did — an attributed recovery on a sure thing was going to happen anyway.

  * **Incrementality** is the holdout comparison, computed in `yukti.eval`.
    Holdout cases have no actions at all, so 100% of their recoveries are
    organic *by construction* rather than by inference. That structural fact is
    what makes the holdout a valid denominator.

Attribution alone is what every competitor reports. The holdout is what makes
the number honest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from yukti.domain.enums import Arm
from yukti.domain.ids import new_id

# How long after an action a capture may still be credited to it. 72h is the
# window the industry uses for dunning, and it is stored per-outcome rather than
# assumed globally so the eval harness can re-run under a different window
# without regenerating anything.
DEFAULT_ATTRIBUTION_WINDOW_H = 72


@dataclass(frozen=True, slots=True)
class Attribution:
    case_id: str
    action_id: str | None      # None = organic
    outcome: str
    recovered_paise: int
    window_h: int

    @property
    def is_organic(self) -> bool:
        return self.action_id is None


def attribute_capture(
    conn: psycopg.Connection,
    obligation_id: str,
    captured_at: datetime,
    amount_paise: int,
    window_h: int = DEFAULT_ATTRIBUTION_WINDOW_H,
) -> Attribution | None:
    """Credit a capture to the action that plausibly caused it, or to nobody."""
    case = conn.execute(
        "SELECT id, arm FROM recovery_case WHERE obligation_id = %s "
        "ORDER BY opened_at DESC LIMIT 1",
        (obligation_id,),
    ).fetchone()
    if case is None:
        return None

    # The most recent dispatched action still inside its window. Actions that
    # merely contact the customer and actions that move money both count: either
    # can plausibly precipitate a payment.
    cutoff = captured_at - timedelta(hours=window_h)
    action = conn.execute(
        """
        SELECT id FROM recovery_action
         WHERE case_id = %s
           AND dispatched_at IS NOT NULL
           AND dispatched_at <= %s
           AND dispatched_at >= %s
           AND status = 'dispatched'
         ORDER BY dispatched_at DESC
         LIMIT 1
        """,
        (case["id"], captured_at, cutoff),
    ).fetchone()

    # A holdout case that somehow carries an action is a containment failure in
    # the dispatcher, not a data quirk: it silently corrupts the denominator and
    # every lift number computed from it. Fail loudly.
    if case["arm"] == Arm.HOLDOUT.value and action is not None:
        raise AssertionError(
            f"holdout case {case['id']} has a dispatched action {action['id']}; "
            "the holdout is contaminated and lift is no longer measurable"
        )

    return Attribution(
        case_id=case["id"],
        action_id=action["id"] if action else None,
        outcome="recovered",
        recovered_paise=amount_paise,
        window_h=window_h,
    )


def record_outcome(conn: psycopg.Connection, attr: Attribution) -> str | None:
    """Persist an outcome, once per case.

    Idempotent by construction: a redelivered capture that survives the earlier
    dedup layers must not double-count recovered revenue, which would inflate
    exactly the headline metric.
    """
    existing = conn.execute(
        "SELECT id FROM recovery_outcome WHERE case_id = %s LIMIT 1", (attr.case_id,)
    ).fetchone()
    if existing:
        return None

    oid = new_id("out")
    conn.execute(
        """
        INSERT INTO recovery_outcome
            (id, case_id, action_id, outcome, recovered_paise, attribution_window_h)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (oid, attr.case_id, attr.action_id, attr.outcome,
         attr.recovered_paise, attr.window_h),
    )
    return oid
