"""Transactional outbox relay.

The problem it solves: a decision must be persisted AND published. Doing both
directly is a dual write with no atomicity — a crash between them either loses
the event (published never happened) or emits an event for a decision that was
rolled back. Neither is acceptable when the event authorises a debit.

Instead the decision and its outbox row commit in ONE Postgres transaction, and
this relay drains the outbox to Kafka afterwards. Publication becomes
at-least-once, which the consumer side already handles: every consumer in Yukti
is idempotent, so a duplicate publish is a no-op rather than a second debit.

Rows are claimed with SELECT ... FOR UPDATE SKIP LOCKED so several relay
instances can drain concurrently without handing the same row to two of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from confluent_kafka import Producer

from yukti.config import settings


@dataclass(slots=True)
class RelayStats:
    published: int = 0
    failed: int = 0


def enqueue(
    conn: psycopg.Connection, topic: str, partition_key: str, payload: dict
) -> None:
    """Write an outbox row. MUST be called inside the caller's transaction.

    This function deliberately does not commit. Committing here would recreate
    the dual write it exists to prevent.
    """
    conn.execute(
        "INSERT INTO outbox (topic, partition_key, payload) VALUES (%s, %s, %s)",
        (topic, partition_key, json.dumps(payload, default=str)),
    )


class OutboxRelay:
    def __init__(self, conn: psycopg.Connection, producer: Producer | None = None) -> None:
        self.conn = conn
        self.producer = producer or Producer({
            "bootstrap.servers": settings().kafka_bootstrap,
            "enable.idempotence": True,
            "acks": "all",
            "linger.ms": 20,
        })

    def drain(self, batch_size: int = 500) -> RelayStats:
        """Publish one batch of unpublished rows."""
        stats = RelayStats()

        rows = self.conn.execute(
            """
            SELECT id, topic, partition_key, payload
              FROM outbox
             WHERE published_at IS NULL
             ORDER BY id
             LIMIT %s
             FOR UPDATE SKIP LOCKED
            """,
            (batch_size,),
        ).fetchall()
        if not rows:
            return stats

        errors: list[str] = []

        def on_delivery(err, _msg):
            if err is not None:
                errors.append(str(err))

        for row in rows:
            self.producer.produce(
                topic=row["topic"],
                key=row["partition_key"].encode(),
                value=json.dumps(row["payload"], default=str).encode(),
                on_delivery=on_delivery,
            )
        self.producer.flush(30)

        if errors:
            # Mark nothing. The rows stay unpublished and the next drain retries
            # them. Marking optimistically would lose events on a broker blip,
            # and a lost decision event is unrecoverable.
            stats.failed = len(rows)
            self.conn.rollback()
            return stats

        # published_at is the checkpoint. It is set only after the broker has
        # acked every record in the batch, so a crash mid-drain replays the
        # batch rather than skipping it.
        self.conn.execute(
            "UPDATE outbox SET published_at = %s WHERE id = ANY(%s)",
            (datetime.now(UTC), [r["id"] for r in rows]),
        )
        self.conn.commit()
        stats.published = len(rows)
        return stats

    def drain_all(self, batch_size: int = 500, max_batches: int = 1000) -> RelayStats:
        total = RelayStats()
        for _ in range(max_batches):
            s = self.drain(batch_size)
            total.published += s.published
            total.failed += s.failed
            if s.published == 0:
                break
        return total


def pending_count(conn: psycopg.Connection) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM outbox WHERE published_at IS NULL"
    ).fetchone()["n"]
