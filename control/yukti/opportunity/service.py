"""Opportunity formation: payment events become durable recovery cases.

This is where at-least-once, out-of-order delivery is made safe. Four layers,
in increasing cost and decreasing frequency:

  1. Redis dedup set        — fast path, catches the common redelivery
  2. processed_event table  — durable, survives a Redis flush
  3. version check          — orders concurrent updates to the same aggregate
  4. unique index           — the last line of defence, enforced by the database

Only the fourth actually protects money, and it is the one that cannot be
bypassed by a bug in the first three. That layering is deliberate: the cheap
checks exist to keep the expensive one from being hit constantly, not to be
trusted on their own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
import redis

from yukti.config import settings
from yukti.domain.enums import Arm, CaseState, ObligationKind
from yukti.domain.fsm import check_version
from yukti.domain.ids import case_id, trace_id

# Events that create or advance a recovery opportunity. `payment.captured` is
# consumed too, but it closes cases rather than opening them.
OPPORTUNITY_EVENTS = {
    "payment.failed", "subscription.pending", "cart.abandoned", "invoice.overdue",
}
RESOLUTION_EVENTS = {"payment.captured"}

DEDUP_TTL_SECONDS = 7 * 24 * 3600


@dataclass(slots=True)
class IngestResult:
    """What happened to one event. Every field is a metric the console shows."""

    opened: int = 0
    resolved: int = 0
    duplicate: int = 0
    superseded: int = 0
    ignored: int = 0

    def __add__(self, other: IngestResult) -> IngestResult:
        return IngestResult(
            self.opened + other.opened,
            self.resolved + other.resolved,
            self.duplicate + other.duplicate,
            self.superseded + other.superseded,
            self.ignored + other.ignored,
        )


class OpportunityService:
    def __init__(self, conn: psycopg.Connection, rds: redis.Redis | None = None) -> None:
        self.conn = conn
        self.redis = rds if rds is not None else redis.from_url(settings().redis_url)

    # -- dedup ---------------------------------------------------------------

    def _seen_recently(self, event_id: str) -> bool:
        """Fast-path dedup. SET NX returns False when the key already existed."""
        try:
            return not self.redis.set(f"yukti:evt:{event_id}", "1",
                                      nx=True, ex=DEDUP_TTL_SECONDS)
        except redis.RedisError:
            # Redis is a cache, not a source of truth. If it is unavailable we
            # fall through to the durable check rather than failing the event —
            # dropping a real payment event is far worse than a slower path.
            return False

    def _record_durably(self, event_id: str, source: str) -> bool:
        """Durable dedup. Returns True if this is the first time we've seen it."""
        cur = self.conn.execute(
            "INSERT INTO processed_event (event_id, source) VALUES (%s, %s) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            (event_id, source),
        )
        return cur.fetchone() is not None

    # -- ingest --------------------------------------------------------------

    def ingest(self, event: dict[str, Any]) -> IngestResult:
        etype = event.get("event_type", "")
        eid = event.get("event_id")

        if etype not in OPPORTUNITY_EVENTS and etype not in RESOLUTION_EVENTS:
            return IngestResult(ignored=1)
        if not eid:
            return IngestResult(ignored=1)

        if self._seen_recently(eid) or not self._record_durably(eid, "kafka"):
            return IngestResult(duplicate=1)

        if etype in RESOLUTION_EVENTS:
            return self._resolve(event)
        return self._open(event)

    def _open(self, event: dict[str, Any]) -> IngestResult:
        oid = event["obligation_id"]
        row = self.conn.execute(
            "SELECT id, state, version FROM obligation WHERE id = %s", (oid,)
        ).fetchone()
        if row is None:
            # An event for an obligation we have never seen. Real systems get
            # these when a webhook overtakes its own backfill; we ignore rather
            # than fabricate an obligation from a webhook payload.
            return IngestResult(ignored=1)

        verdict = check_version(row["version"], int(event.get("version", 1)) + 1)
        if verdict.superseded:
            return IngestResult(superseded=1)

        if row["state"] != "open":
            return IngestResult(ignored=1)

        # Deterministic arm assignment: a pure function of (salt, customer), so
        # it reproduces exactly on replay and cannot drift between arms. Keyed
        # on the CUSTOMER, not the case, so one customer is never half-held-out
        # — that would contaminate the fatigue measurement across their cases.
        arm = self._assign_arm(event["merchant_id"], event["customer_id"])

        try:
            # A SAVEPOINT, not a bare execute. Postgres aborts the whole
            # transaction on a constraint violation, so without this the
            # rollback would also discard unrelated work already done in this
            # transaction — including a case opened for a different obligation
            # moments earlier. The savepoint scopes the failure to this insert.
            with self.conn.transaction():
                self.conn.execute(
                    """
                    INSERT INTO recovery_case
                        (id, obligation_id, merchant_id, customer_id, state, arm, opened_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (case_id(), oid, event["merchant_id"], event["customer_id"],
                     CaseState.OPEN.value, arm.value, _ts(event["ts"])),
                )
        except psycopg.errors.UniqueViolation:
            # The partial unique index caught a live case for this obligation.
            # This is the layer that actually stops a duplicated webhook from
            # causing a second round of work on the same customer, and the only
            # one a bug in the cheaper checks above cannot bypass.
            return IngestResult(duplicate=1)

        return IngestResult(opened=1)

    def _resolve(self, event: dict[str, Any]) -> IngestResult:
        """A capture arrived. Close any live case for that obligation."""
        cur = self.conn.execute(
            """
            UPDATE recovery_case
               SET state = %s, closed_at = %s, version = version + 1
             WHERE obligation_id = %s
               AND state NOT IN ('stopped', 'recovered', 'lost')
            RETURNING id
            """,
            (CaseState.RECOVERED.value, _ts(event["ts"]), event["obligation_id"]),
        )
        return IngestResult(resolved=1) if cur.fetchone() else IngestResult(ignored=1)

    def _assign_arm(self, merchant_id: str, customer_id: str) -> Arm:
        import hashlib

        cfg = settings()
        h = hashlib.blake2b(
            f"{merchant_id}|{customer_id}".encode(), key=b"yukti-holdout", digest_size=8
        ).digest()
        bucket = int.from_bytes(h, "big") % 10_000
        return Arm.HOLDOUT if bucket < cfg.holdout_pct * 100 else Arm.TREATMENT


def _ts(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
