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

from confluent_kafka import Consumer, KafkaError, KafkaException
from rich.console import Console

from yukti.config import settings
from yukti.opportunity.service import IngestResult, OpportunityService
from yukti.store.db import connect

console = Console()


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
                event = json.loads(msg.value())
                totals = totals + svc.ingest(event)
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
            f"superseded=[yellow]{totals.superseded:,}[/] ignored={totals.ignored:,}"
        )
    return totals
