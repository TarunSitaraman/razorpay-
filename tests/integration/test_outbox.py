"""Transactional outbox: the mechanism that removes the Postgres/Kafka dual write."""

from __future__ import annotations

import pytest
from yukti.dispatch.outbox import OutboxRelay, enqueue, pending_count

pytestmark = pytest.mark.integration


class FakeProducer:
    """Records what was produced; can be told to fail the whole batch."""

    def __init__(self, fail: bool = False) -> None:
        self.produced: list[tuple[str, bytes]] = []
        self.fail = fail

    def produce(self, topic, key, value, on_delivery=None):
        if self.fail:
            if on_delivery:
                on_delivery("broker unavailable", None)
            return
        self.produced.append((topic, value))
        if on_delivery:
            on_delivery(None, None)

    def flush(self, _timeout=None):
        return 0


class TestAtomicity:
    def test_rollback_discards_both_the_row_and_the_event(self, conn, merchant):
        # The whole point: if the decision rolls back, the event never exists.
        before = pending_count(conn)
        enqueue(conn, "recovery.actions", merchant, {"kind": "message"})
        conn.rollback()
        assert pending_count(conn) == before

    def test_commit_makes_the_event_visible_to_the_relay(self, conn, merchant):
        enqueue(conn, "recovery.actions", merchant, {"kind": "message"})
        conn.commit()
        assert pending_count(conn) >= 1


class TestDraining:
    def test_drain_publishes_and_checkpoints(self, conn, merchant):
        for i in range(3):
            enqueue(conn, "recovery.actions", merchant, {"seq": i})
        conn.commit()

        producer = FakeProducer()
        stats = OutboxRelay(conn, producer).drain_all()

        assert stats.published >= 3
        assert pending_count(conn) == 0

    def test_second_drain_is_a_no_op(self, conn, merchant):
        enqueue(conn, "recovery.actions", merchant, {"x": 1})
        conn.commit()
        relay = OutboxRelay(conn, FakeProducer())
        relay.drain_all()

        again = relay.drain_all()
        assert again.published == 0


class TestFailureHandling:
    def test_broker_failure_leaves_rows_unpublished_for_retry(self, conn, merchant):
        # Marking optimistically would lose events on a broker blip, and a lost
        # decision event is unrecoverable.
        enqueue(conn, "recovery.actions", merchant, {"x": 1})
        conn.commit()
        before = pending_count(conn)

        stats = OutboxRelay(conn, FakeProducer(fail=True)).drain()

        assert stats.published == 0 and stats.failed > 0
        assert pending_count(conn) == before

    def test_recovers_once_the_broker_returns(self, conn, merchant):
        enqueue(conn, "recovery.actions", merchant, {"x": 1})
        conn.commit()
        OutboxRelay(conn, FakeProducer(fail=True)).drain()

        good = FakeProducer()
        stats = OutboxRelay(conn, good).drain_all()

        assert stats.published >= 1
        assert pending_count(conn) == 0
