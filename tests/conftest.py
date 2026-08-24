"""Shared test fixtures. These require the local stack (`make up`).

Lives at the `tests/` root rather than under `integration/` so the chaos suite
sees the same fixtures. Chaos tests break real seams — a dead adapter, a missing
model, a replayed cycle — and they need the same database and merchant setup the
integration tests use; duplicating that would let the two drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `tests/integration` on the path so any suite can reuse the case builder that
# lives beside the spine tests (`from test_plan_cycle import _make_case`).
# pytest only adds a test module's OWN directory, so a sibling suite importing
# it would otherwise fail at collection.
sys.path.insert(0, str(Path(__file__).parent / "integration"))
from yukti.domain.ids import customer_id, merchant_id, obligation_id
from yukti.store.db import connect


@pytest.fixture
def conn():
    """A connection whose work is rolled back at the end of each test."""
    c = connect()
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def fake_redis():
    """In-memory stand-in so dedup tests exercise the durable path too."""

    class FakeRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:
                return None
            self.store[key] = value
            return True

    return FakeRedis()


# Tables holding per-merchant rows, in reverse foreign-key order. Used by the
# `merchant` fixture's teardown.
_MERCHANT_SCOPED = (
    ("recovery_outcome", "case_id IN (SELECT id FROM recovery_case WHERE merchant_id = %s)"),
    ("recovery_action", "case_id IN (SELECT id FROM recovery_case WHERE merchant_id = %s)"),
    ("policy_evaluation", "decision_id IN (SELECT id FROM agent_decision WHERE case_id "
                          "IN (SELECT id FROM recovery_case WHERE merchant_id = %s))"),
    ("agent_decision", "case_id IN (SELECT id FROM recovery_case WHERE merchant_id = %s)"),
    ("recovery_case", "merchant_id = %s"),
    ("agent_run", "merchant_id = %s"),
    ("promise_to_pay", "obligation_id IN (SELECT id FROM obligation WHERE merchant_id = %s)"),
    ("payment_attempt", "obligation_id IN (SELECT id FROM obligation WHERE merchant_id = %s)"),
    ("obligation", "merchant_id = %s"),
    ("customer", "merchant_id = %s"),
    ("policy_pack", "merchant_id = %s"),
    ("budget_ledger", "merchant_id = %s"),
    ("audit_event", "merchant_id = %s"),
    ("experiment", "merchant_id = %s"),
    ("merchant", "id = %s"),
)


@pytest.fixture
def merchant(conn):
    """A throwaway merchant, cleaned up afterwards.

    Rolling back the connection is not enough on its own: `plan_cycle` commits,
    because committing is what it does in production and a test that prevented
    that would not be testing the real path. So the fixture deletes its own
    merchant's rows at teardown instead.

    Without this, every integration run leaves stopped cases and audit rows in
    the development database, and the console, the metrics and any later
    measurement quietly read test data as if it were real.
    """
    mid = merchant_id()
    conn.execute(
        "INSERT INTO merchant (id, name, segment) VALUES (%s, %s, %s)",
        (mid, "Test Merchant", "d2c_subscription"),
    )
    yield mid

    conn.rollback()
    for table, predicate in _MERCHANT_SCOPED:
        conn.execute(f"DELETE FROM {table} WHERE {predicate}", (mid,))
    conn.commit()


@pytest.fixture
def customer(conn, merchant):
    cid = customer_id()
    conn.execute(
        "INSERT INTO customer (id, merchant_id, ltv_band) VALUES (%s, %s, %s)",
        (cid, merchant, "mid"),
    )
    return cid


@pytest.fixture
def obligation(conn, merchant, customer):
    from datetime import datetime

    oid = obligation_id()
    conn.execute(
        """
        INSERT INTO obligation
            (id, merchant_id, customer_id, kind, amount_paise, due_at, state, version)
        VALUES (%s, %s, %s, 'subscription_cycle', 250000, %s, 'open', 1)
        """,
        (oid, merchant, customer, datetime(2026, 5, 15, 9, 0)),
    )
    return oid
