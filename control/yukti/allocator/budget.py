"""Budget ledgers — what a merchant authorised, and what has been spent.

A budget is a hard constraint, not a target. The merchant authorised a number,
and exceeding it is a breach of what they agreed to, regardless of how good the
opportunity looked. So the guarantee here is enforced by the database rather
than by the allocator's arithmetic:

    UPDATE budget_ledger SET consumed_val = consumed_val + %s
     WHERE ... AND consumed_val + %s <= limit_val
    RETURNING consumed_val

A row that would breach the limit does not update and returns nothing. Two
planners racing on the same merchant cannot both succeed, because the second
one's check runs against the first one's committed value under row lock. A
read-then-write in Python would pass both.

Ledgers rather than counters, per the schema comment: consumption is auditable
and a replay can reconstruct exactly what was affordable at each point in time,
which the evaluation harness needs to reproduce a decision it made a week ago.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

# Applied when a merchant has no ledger row for a window — a new merchant, or a
# day the seeder did not cover. Conservative on purpose: a missing budget should
# mean "act cautiously", never "act without limit".
DEFAULT_CONTACT_LIMIT = 50
DEFAULT_DISCOUNT_LIMIT_PAISE = 50_000_00


@dataclass(frozen=True, slots=True)
class BudgetState:
    kind: str
    limit_val: int
    consumed_val: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit_val - self.consumed_val)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


class BudgetExceeded(RuntimeError):
    """A spend was refused because it would breach the authorised limit."""


def _default_limit(kind: str) -> int:
    return DEFAULT_CONTACT_LIMIT if kind == "contact" else DEFAULT_DISCOUNT_LIMIT_PAISE


def load(
    conn: psycopg.Connection, merchant_id: str, kind: str, window: date
) -> BudgetState:
    """Read one budget, creating a conservative default row if absent."""
    row = conn.execute(
        "SELECT kind, limit_val, consumed_val FROM budget_ledger "
        " WHERE merchant_id = %s AND kind = %s AND window_start = %s",
        (merchant_id, kind, window),
    ).fetchone()
    if row is not None:
        return BudgetState(row["kind"], int(row["limit_val"]), int(row["consumed_val"]))

    conn.execute(
        "INSERT INTO budget_ledger (merchant_id, kind, window_start, limit_val) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (merchant_id, kind, window, _default_limit(kind)),
    )
    return BudgetState(kind, _default_limit(kind), 0)


def load_all(
    conn: psycopg.Connection, merchant_id: str, window: date
) -> dict[str, BudgetState]:
    return {k: load(conn, merchant_id, k, window) for k in ("contact", "discount")}


def spend(
    conn: psycopg.Connection, merchant_id: str, kind: str, window: date, amount: int
) -> int:
    """Consume from a budget atomically. Returns the new consumed total.

    Raises `BudgetExceeded` rather than clamping. A caller that asked to spend
    more than remains has a bug — the allocator is supposed to have sized the
    plan to the budget — and silently spending less would hide it while
    producing a plan that no longer matches what was dispatched.
    """
    if amount < 0:
        raise ValueError("spend amount must be non-negative")
    if amount == 0:
        return load(conn, merchant_id, kind, window).consumed_val

    load(conn, merchant_id, kind, window)  # ensure the row exists

    row = conn.execute(
        """
        UPDATE budget_ledger
           SET consumed_val = consumed_val + %(amount)s
         WHERE merchant_id = %(merchant)s
           AND kind = %(kind)s
           AND window_start = %(window)s
           AND consumed_val + %(amount)s <= limit_val
        RETURNING consumed_val, limit_val
        """,
        {"amount": amount, "merchant": merchant_id, "kind": kind, "window": window},
    ).fetchone()

    if row is None:
        state = load(conn, merchant_id, kind, window)
        raise BudgetExceeded(
            f"{merchant_id} {kind} budget for {window}: asked for {amount}, "
            f"{state.remaining} of {state.limit_val} remains"
        )
    return int(row["consumed_val"])


def release(
    conn: psycopg.Connection, merchant_id: str, kind: str, window: date, amount: int
) -> int:
    """Return unspent budget — a dispatch that failed after the spend was taken.

    Floored at zero so a double release cannot manufacture budget. The failure
    mode this guards against is a retry path that releases once per attempt.
    """
    row = conn.execute(
        """
        UPDATE budget_ledger
           SET consumed_val = greatest(0, consumed_val - %s)
         WHERE merchant_id = %s AND kind = %s AND window_start = %s
        RETURNING consumed_val
        """,
        (amount, merchant_id, kind, window),
    ).fetchone()
    return int(row["consumed_val"]) if row else 0
