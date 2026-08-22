"""Replay the Parquet event log into Kafka in timestamp order.

Replay is not a demo convenience — it is load-bearing for the evaluation. Every
baseline arm and the agent are scored by replaying the *same* stream from
offset 0, so the comparison is paired at the event level. A queue that destroys
messages on consumption could not do this; a log can.

Events are keyed by merchant_id so that per-merchant ordering is preserved
while distinct merchants process in parallel across partitions.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pyarrow.parquet as pq
from confluent_kafka import Producer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from yukti.config import settings

from yukti_datagen.persist import DATA_DIR

console = Console()


def _json_default(o: object) -> str:
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def replay(speed: float = 200.0, limit: int = 0, topic: str | None = None) -> int:
    """Publish events to Kafka, pacing wall-clock time by ``speed``.

    speed=0 replays as fast as the broker accepts, which is what the evaluation
    harness uses. A finite speed is for the live demo, where watching decisions
    arrive in order is the point.
    """
    cfg = settings()
    topic = topic or cfg.topic_payments
    path = DATA_DIR / "events.parquet"
    if not path.exists():
        raise SystemExit(f"no event log at {path} — run `make seed` first")

    rows = pq.read_table(path).to_pylist()
    if limit:
        rows = rows[:limit]
    if not rows:
        return 0

    producer = Producer({
        "bootstrap.servers": cfg.kafka_bootstrap,
        "linger.ms": 20,
        "compression.type": "lz4",
        # Idempotent producer: a retried publish after a broker ack timeout must
        # not duplicate the event. The consumer is idempotent too, but removing
        # a whole class of duplicates at the source keeps the dedup metric
        # meaningful rather than noisy.
        "enable.idempotence": True,
        "acks": "all",
    })

    delivered = 0
    failures: list[str] = []

    def on_delivery(err, _msg):
        nonlocal delivered
        if err is not None:
            failures.append(str(err))
        else:
            delivered += 1

    t0_stream = rows[0]["ts"]
    t0_wall = time.time()
    published = 0

    with Progress(
        TextColumn("[cyan]replaying[/]"), BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,}"), TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("replay", total=len(rows))
        for row in rows:
            if speed > 0:
                stream_elapsed = (row["ts"] - t0_stream).total_seconds()
                target = t0_wall + stream_elapsed / speed
                drift = target - time.time()
                if drift > 0:
                    time.sleep(drift)

            producer.produce(
                topic=topic,
                key=row["merchant_id"].encode(),   # per-merchant ordering
                value=json.dumps(row, default=_json_default).encode(),
                headers=[
                    ("event_id", row["event_id"].encode()),
                    ("event_type", row["event_type"].encode()),
                ],
                on_delivery=on_delivery,
            )
            published += 1
            progress.update(task, advance=1)
            # Serve delivery callbacks without blocking the pacing loop.
            if published % 500 == 0:
                producer.poll(0)

    producer.flush(30)
    if failures:
        console.print(f"[red]{len(failures)} delivery failures[/]; first: {failures[0]}")
    console.print(f"  published [bold]{delivered:,}[/] events to [green]{topic}[/]")
    return delivered
