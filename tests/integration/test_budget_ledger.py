"""Budgets are hard limits, enforced by the database rather than by arithmetic.

The failure this guards against is subtle: a read-then-write in Python passes
its own check and still overspends when two planners run concurrently, because
both read the same value before either wrote. The conditional UPDATE closes that
window, and these tests are written to fail if it is ever replaced with a SELECT
followed by an UPDATE.
"""

from __future__ import annotations

from datetime import date

import pytest

from yukti.allocator import budget
from yukti.store.db import connect

WINDOW = date(2026, 7, 20)


@pytest.fixture
def ledger(conn, merchant):
    conn.execute(
        "INSERT INTO budget_ledger (merchant_id, kind, window_start, limit_val) "
        "VALUES (%s, 'contact', %s, 100), (%s, 'discount', %s, 500000)",
        (merchant, WINDOW, merchant, WINDOW),
    )
    return merchant


def test_spend_reduces_what_remains(conn, ledger):
    budget.spend(conn, ledger, "contact", WINDOW, 30)
    assert budget.load(conn, ledger, "contact", WINDOW).remaining == 70


def test_spending_exactly_the_limit_is_allowed(conn, ledger):
    budget.spend(conn, ledger, "contact", WINDOW, 100)
    state = budget.load(conn, ledger, "contact", WINDOW)
    assert state.remaining == 0
    assert state.exhausted


def test_overspending_is_refused_not_clamped(conn, ledger):
    """Clamping would silently dispatch fewer actions than the plan recorded."""
    budget.spend(conn, ledger, "contact", WINDOW, 90)
    with pytest.raises(budget.BudgetExceeded) as exc:
        budget.spend(conn, ledger, "contact", WINDOW, 20)
    assert "10 of 100 remains" in str(exc.value)
    assert budget.load(conn, ledger, "contact", WINDOW).consumed_val == 90


def test_a_missing_ledger_defaults_conservatively(conn, merchant):
    """No row must mean 'be careful', never 'no limit'."""
    state = budget.load(conn, merchant, "contact", date(2027, 1, 1))
    assert state.limit_val == budget.DEFAULT_CONTACT_LIMIT
    assert state.remaining == budget.DEFAULT_CONTACT_LIMIT


def test_release_returns_budget_but_cannot_manufacture_it(conn, ledger):
    """A retry path that releases once per attempt must not create budget."""
    budget.spend(conn, ledger, "contact", WINDOW, 10)
    budget.release(conn, ledger, "contact", WINDOW, 10)
    budget.release(conn, ledger, "contact", WINDOW, 10)
    assert budget.load(conn, ledger, "contact", WINDOW).consumed_val == 0


def test_concurrent_spends_cannot_both_win(conn, ledger):
    """Two connections, one budget. The database decides, not the callers.

    Both would pass a read-then-check against the same starting value. Only one
    can pass the conditional UPDATE.
    """
    conn.commit()   # make the ledger visible to the second connection

    other = connect()
    try:
        budget.spend(conn, ledger, "contact", WINDOW, 60)
        conn.commit()
        with pytest.raises(budget.BudgetExceeded):
            budget.spend(other, ledger, "contact", WINDOW, 60)
        other.commit()

        assert budget.load(conn, ledger, "contact", WINDOW).consumed_val == 60
    finally:
        other.close()
        conn.execute("DELETE FROM budget_ledger WHERE merchant_id = %s", (ledger,))
        conn.commit()


def test_zero_spend_is_a_no_op(conn, ledger):
    assert budget.spend(conn, ledger, "contact", WINDOW, 0) == 0


def test_negative_spend_is_rejected(conn, ledger):
    with pytest.raises(ValueError):
        budget.spend(conn, ledger, "contact", WINDOW, -5)
