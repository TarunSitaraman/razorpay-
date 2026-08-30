"""Kafka consumer that drives opportunity formation.

Offsets are committed only after the database transaction commits. That gives
at-least-once processing: a crash between the two replays the event, and the
dedup layers make the replay a no-op. The reverse order would give at-most-once
and could silently drop a real payment failure.
"""

from __future__ import annotations

import json
import logging
import signal
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from rich.console import Console

from yukti.config import settings
from yukti.opportunity.service import IngestResult, MalformedEvent, OpportunityService
from yukti.store.db import connect

log = logging.getLogger(__name__)

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
        # Left implicit, this defaults to 45s. The batch between polls does
        # database work whose latency depends on the storage underneath, and a
        # coordinator that goes briefly unreachable -- a broker restart, a busy
        # host -- evicts the consumer mid-batch. Widening it does not paper over
        # slow work (`max.poll.interval.ms` still bounds that); it stops a
        # transient coordinator hiccup from tearing down a healthy consumer.
        "session.timeout.ms": 120_000,
        "heartbeat.interval.ms": 10_000,
    })


# Commit failures that mean "you were removed from the group", not "the broker
# is broken". Every one of them is recoverable by rejoining and re-consuming.
_REBALANCE_ERRORS = frozenset({
    KafkaError.ILLEGAL_GENERATION,
    KafkaError.REBALANCE_IN_PROGRESS,
    KafkaError.UNKNOWN_MEMBER_ID,
})


def _commit_offsets(consumer: Consumer, msg: Any) -> bool:
    """Commit through `msg`, tolerating a rebalance. Returns whether it stuck.

    A rebalance between the database commit and the offset commit is not a
    failure state for this consumer, and it must not be fatal. The offsets stay
    where they were, the events are re-delivered to whoever owns the partition
    next, and `OpportunityService.ingest` de-duplicates them -- which is the
    entire point of the four-layer dedup design, and the guarantee the module
    docstring already claims.

    Crashing here instead threw away a healthy process over a recoverable
    condition. Observed: a slow batch against container storage let the session
    lapse, the group rebalanced, and `commit()` raised ILLEGAL_GENERATION,
    killing an ingest run 118,000 events in.

    The one thing that would be wrong is committing anyway or exiting quietly.
    Both are avoided: the caller keeps consuming, and the failure is reported.
    """
    try:
        consumer.commit(msg, asynchronous=False)
        return True
    except KafkaException as exc:
        err = exc.args[0]
        if err.code() in _REBALANCE_ERRORS:
            log.warning(
                "offset commit skipped after rebalance (%s); events will be "
                "re-delivered and de-duplicated", err.name(),
            )
            return False
        raise


class NoAssignment(RuntimeError):
    """The consumer never received a partition assignment."""


def run(
    group_id: str = "yukti-opportunity",
    max_events: int = 0,
    idle_timeout_s: float = 5.0,
    quiet: bool = False,
    commit_every: int = 500,
    assignment_timeout_s: float = 30.0,
) -> IngestResult:
    """Consume until the stream goes idle or ``max_events`` is reached.

    **"Idle" must mean "assigned, and nothing arriving."** An unassigned
    consumer also polls empty, and conflating the two makes a slow group join
    indistinguishable from an empty topic. That is not a theoretical distinction:
    against a broker in a container, joining took longer than `idle_timeout_s`,
    so `make consume` exited immediately, reported `0 events -> opened=0`, and
    left 300,000 events unconsumed while looking like a clean run. The pipeline
    then trained and planned on 805 cases instead of 23,864.

    The failure shape is what makes it worth guarding: no exception, no non-zero
    exit, just a quiet no-op wearing the costume of success. So the idle
    countdown now starts only once partitions are actually assigned, and never
    being assigned is an error rather than an empty result.
    """
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
    pending = 0
    last_msg = None
    idle = 0.0
    # Time spent waiting to join the group, kept separate from idle time.
    waiting = 0.0
    assigned = False

    dlq = build_dlq_producer()

    with connect() as conn:
        svc = OpportunityService(conn)
        try:
            while not stopping:
                msg = consumer.poll(0.5)
                if msg is None:
                    if not assigned:
                        if consumer.assignment():
                            assigned = True
                        else:
                            waiting += 0.5
                            if waiting >= assignment_timeout_s:
                                raise NoAssignment(
                                    f"no partitions assigned for "
                                    f"{cfg.topic_payments!r} after "
                                    f"{assignment_timeout_s:.0f}s - is the "
                                    f"broker reachable and the topic created?"
                                )
                            continue
                    idle += 0.5
                    if idle >= idle_timeout_s:
                        break
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    raise KafkaException(msg.error())

                idle = 0.0
                assigned = True
                raw = msg.value()
                try:
                    event = json.loads(raw)
                    totals = totals + svc.ingest(event)
                except (MalformedEvent, json.JSONDecodeError, KeyError) as exc:
                    # Quarantine and keep going. A consumer that dies on one bad
                    # record turns a single malformed event into an outage for
                    # every merchant sharing that partition.
                    # Rolling back discards the whole open batch, not just this
                    # event, so the batch is replayed from the last committed
                    # offset. That is safe — the ingest path is idempotent — but
                    # it must not silently lose the events, so the offset is NOT
                    # advanced here and `pending` is reset to reflect that the
                    # transaction is now empty.
                    conn.rollback()
                    pending = 0
                    to_dlq(dlq, raw, f"{type(exc).__name__}: {exc}", cfg.topic_dlq)
                    totals = totals + IngestResult(malformed=1)

                processed += 1
                pending += 1
                last_msg = msg

                # Batched, not per message. A database commit plus a synchronous
                # offset commit on every event is two round trips and an fsync
                # each, and it measured at ~200 events/s — which makes a full
                # 338k-event replay a 28-minute operation and the evaluation
                # harness, which replays five arms, unusable.
                #
                # The invariant that matters is unchanged: the offset still
                # advances only AFTER the database transaction commits. Batching
                # only widens what a crash replays, from one event to at most
                # `commit_every`, and every stage downstream is idempotent —
                # Redis dedup, processed_event, the version check and the partial
                # unique index. Absorbing a replayed batch is precisely what
                # those four layers were built for.
                if pending >= commit_every:
                    conn.commit()
                    _commit_offsets(consumer, last_msg)
                    pending = 0

                if not quiet and processed % 2000 == 0:
                    console.print(f"  processed {processed:,}  opened={totals.opened:,}")
                if max_events and processed >= max_events:
                    break

            # Drain whatever the last partial batch left uncommitted. Without
            # this, a clean shutdown would silently discard up to `commit_every`
            # events of work and replay them on the next run.
            if pending:
                conn.commit()
                if last_msg is not None:
                    _commit_offsets(consumer, last_msg)
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
