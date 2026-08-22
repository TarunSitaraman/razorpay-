"""Replay the event log through the sandbox as signed webhooks.

Two replay paths exist on purpose:

  * `make replay` publishes straight to Kafka. The evaluation harness replays
    90 days across five arms, so it must not pay HTTP and HMAC costs per event.
  * `make replay-webhooks` (this module) drives sandbox -> ingest-gw -> Kafka,
    exercising signature verification, the replay window and edge dedup.

Both consume the same Parquet log, so the fast path stays honest while the
realistic path stays real.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import httpx
import pyarrow.parquet as pq
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from yukti_datagen.persist import DATA_DIR

console = Console()


def replay_webhooks(
    sandbox_url: str = "http://localhost:8081",
    speed: float = 0.0,
    limit: int = 0,
    concurrency: int = 32,
) -> dict[str, int]:
    path = DATA_DIR / "events.parquet"
    if not path.exists():
        raise SystemExit(f"no event log at {path} — run `make seed` first")

    rows = pq.read_table(path).to_pylist()
    if limit:
        rows = rows[:limit]

    stats = {"emitted": 0, "delivered": 0, "failed": 0}
    t0_stream = rows[0]["ts"] if rows else None
    t0_wall = time.time()
    lock = threading.Lock()

    def to_payload(row: dict) -> dict:
        # Coerce every datetime, not just `ts`: the row also carries `due_at`,
        # and json= would raise on any of them. Doing this generically means a
        # new datetime column cannot silently break replay later.
        payload = {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in row.items()
            if k != "ts"
        }
        payload["event_type"] = row["event_type"]
        # The webhook must look freshly minted or the edge's replay window will
        # correctly reject it. Historical position travels in a business field:
        # `created_at` is a transport-level anti-replay control, not a business
        # timestamp, and conflating the two would mean choosing between honest
        # history and a working replay guard.
        payload["occurred_at"] = row["ts"].isoformat()
        return payload

    def send(client: httpx.Client, row: dict) -> None:
        try:
            resp = client.post(f"{sandbox_url}/_sandbox/emit", json=to_payload(row))
            ok = resp.status_code == 200 and resp.json().get("delivered") == 200
        except httpx.HTTPError:
            ok = False
        with lock:
            stats["emitted"] += 1
            stats["delivered" if ok else "failed"] += 1

    with httpx.Client(
        timeout=30.0,
        limits=httpx.Limits(
            max_connections=concurrency, max_keepalive_connections=concurrency
        ),
    ) as client, Progress(
        TextColumn("[cyan]webhook replay[/]"), BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,}"), TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("replay", total=len(rows))
        # Concurrent delivery. This is not only faster — it is more faithful:
        # real webhook fleets deliver in parallel and therefore out of order,
        # which is exactly the condition the opportunity service's version
        # checks exist to handle. A strictly sequential replay would never
        # exercise them.
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = []
            for row in rows:
                if speed > 0 and t0_stream is not None:
                    target = t0_wall + (row["ts"] - t0_stream).total_seconds() / speed
                    drift = target - time.time()
                    if drift > 0:
                        time.sleep(drift)
                futures.append(pool.submit(send, client, row))
                # Bound the queue so a fast producer cannot build an unbounded
                # backlog of pending futures in memory.
                if len(futures) >= concurrency * 8:
                    for f in as_completed(futures):
                        f.result()
                        progress.update(task, advance=1)
                    futures = []
            for f in as_completed(futures):
                f.result()
                progress.update(task, advance=1)

    console.print(
        f"  emitted [bold]{stats['emitted']:,}[/]  "
        f"delivered [green]{stats['delivered']:,}[/]  "
        f"failed [red]{stats['failed']:,}[/]"
    )
    return stats
