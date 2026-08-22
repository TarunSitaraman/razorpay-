"""Integration test fixtures. These require the local stack (`make up`)."""

from __future__ import annotations

import pytest

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


@pytest.fixture
def merchant(conn):
    mid = merchant_id()
    conn.execute(
        "INSERT INTO merchant (id, name, segment) VALUES (%s, %s, %s)",
        (mid, "Test Merchant", "d2c_subscription"),
    )
    return mid


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
