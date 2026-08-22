"""Kafka consumer that drives opportunity formation.

Offsets are committed only after the database transaction commits. That gives
at-least-once processing: a crash between the two replays the event, and the
dedup layers make the replay a no-op. The reverse order would give at-most-once
and could silently drop a real payment failure.
"""

from __future__ import annotations

import json
import signal
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from rich.console import Console

from yukti.config import settings
from yukti.opportunity.service import IngestResult, MalformedEvent, OpportunityService
from yukti.store.db import connect

console = Console()


def build_dlq_producer() -> Producer:
    return Producer({
        "bootstrap.servers": settings().kafka_bootstrap,
        "enable.idempotence": True,
        "acks": "all",
    })


def to_dlq(producer: Producer, raw: bytes, reason: str, topic: str) -> None:
    """Quarantine a message that can never succeed.

    Retrying a structurally invalid event fails identically forever and blocks
    every message behind it on that partition — one poison record would stall a
    merchant's entire stream. The DLQ keeps the partition moving and preserves
    the payload for inspection.
    """
    producer.produce(
        topic=topic,
        value=raw,
        headers=[("dlq_reason", reason.encode()[:512])],
    )
    producer.flush(10)


def build_consumer(group_id: str = "yukti-opportunity") -> Consumer:
    cfg = settings()
    return Consumer({
        "bootstrap.servers": cfg.kafka_bootstrap,
        "group.id": group_id,
        "auto.offset.reset": "earliest",
        # Manual commit: the offset must not advance until the work is durable.
        "enable.auto.commit": False,
        "max.poll.interval.ms": 300_000,
    })


def run(
    group_id: str = "yukti-opportunity",
    max_events: int = 0,
    idle_timeout_s: float = 5.0,
    quiet: bool = False,
) -> IngestResult:
    """Consume until the stream goes idle or ``max_events`` is reached."""
    cfg = settings()
    consumer = build_consumer(group_id)
    consumer.subscribe([cfg.topic_payments])

    stopping = False

    def _stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    totals = IngestResult()
    processed = 0
    idle = 0.0

    dlq = build_dlq_producer()

    with connect() as conn:
        svc = OpportunityService(conn)
        try:
            while not stopping:
                msg = consumer.poll(0.5)
                if msg is None:
                    idle += 0.5
                    if idle >= idle_timeout_s:
                        break
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(msg.error())

                idle = 0.0
                raw = msg.value()
                try:
                    event = json.loads(raw)
                    totals = totals + svc.ingest(event)
                except (MalformedEvent, json.JSONDecodeError, KeyError) as exc:
                    # Quarantine and keep going. A consumer that dies on one bad
                    # record turns a single malformed event into an outage for
                    # every merchant sharing that partition.
                    conn.rollback()
                    to_dlq(dlq, raw, f"{type(exc).__name__}: {exc}", cfg.topic_dlq)
                    totals = totals + IngestResult(malformed=1)

                conn.commit()
                consumer.commit(msg, asynchronous=False)

                processed += 1
                if not quiet and processed % 2000 == 0:
                    console.print(f"  processed {processed:,}  opened={totals.opened:,}")
                if max_events and processed >= max_events:
                    break
        finally:
            consumer.close()

    if not quiet:
        console.print(
            f"  [bold]{processed:,}[/] events -> "
            f"opened=[green]{totals.opened:,}[/] resolved=[cyan]{totals.resolved:,}[/] "
            f"duplicate=[yellow]{totals.duplicate:,}[/] "
            f"superseded=[yellow]{totals.superseded:,}[/] "
            f"malformed=[red]{totals.malformed:,}[/] ignored={totals.ignored:,}"
        )
    return totals
