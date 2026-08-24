"""Dispatch — turning a decided action into an effect, exactly once.

The hardest correctness property in this system is exactly-once *effect* under
at-least-once delivery. A duplicated webhook, a replayed Kafka partition or a
re-run planning cycle must never produce a second discount or a second debit
attempt. Four layers stand between an event and a duplicate charge, and only the
last of them actually protects money:

  1. Redis dedup on the webhook (Go edge)          — cheap, best-effort
  2. processed_event on opportunity formation      — durable, survives a flush
  3. Redis lock here                               — stops two planners racing
  4. recovery_action.idempotency_key UNIQUE        — the guarantee

Layers 1-3 exist to keep layer 4 from being hit constantly. Layer 4 is the one
that cannot be bypassed by a bug in the others, because it is enforced by the
database rather than by our code.

**The key is derived, never minted.** It is a fingerprint of what the action
means — merchant, obligation, kind, channel, the day it is scheduled for, and
the money involved. So a re-run that reaches the same conclusion computes the
same key and collides. A random UUID per attempt would satisfy the unique index
and provide no protection whatsoever, which is the trap this design exists to
avoid.

The day is deliberately the granularity: re-planning the same case on the same
day is a duplicate; deciding to contact the same customer again next week is a
genuinely new action, and the fingerprint must let that through.

Ordering within `dispatch` is chosen so that a crash at any point is safe:

    lock -> write intent (pending) -> COMMIT -> call adapter -> mark dispatched

The intent commits *before* the external call. A crash between them leaves a
`pending` row, which is recoverable — a sweeper retries with the same key and
the vendor's own idempotency returns the original resource. The reverse order
would leave an effect with no record of it, which is not recoverable at all.
"""

from __future__ import annotations

import logging

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
import redis

from yukti import audit
from yukti.config import settings
from yukti.dispatch.adapters import AdapterError, Adapters
from yukti.dispatch.outbox import enqueue
from yukti.dispatch.tools import ActionSpec, ToolOutcome, invoke
from yukti.domain.enums import ActionKind, ActorKind
from yukti.domain.ids import action_id

# Namespaced to this stage, like every other Redis keyspace in Yukti. Sharing a
# prefix between two stages that ask different questions is not a theoretical
# risk here: it silently made the consumer treat 8,775 real events as duplicates
# and open zero cases, and reported a clean run while doing it.
LOCK_KEY_PREFIX = "yukti:dispatch:lock:"
LOCK_TTL_SECONDS = 60


log = logging.getLogger(__name__)


class DispatchLocked(RuntimeError):
    """Another dispatcher holds the lock for this exact action."""


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    action_id: str | None
    dispatched: bool
    duplicate: bool = False
    failed: bool = False
    reason: str = ""
    tool_outcome: ToolOutcome | None = None


def fingerprint(spec: ActionSpec) -> str:
    """Stable identity for an action's *meaning*.

    Two proposals that would have the same effect on the same customer on the
    same day produce the same fingerprint, whatever path produced them. That is
    the whole point: identity comes from semantics, not from the request.
    """
    material = "|".join([
        spec.merchant_id,
        spec.obligation_id,
        spec.action_kind.value,
        spec.channel.value,
        # Day, not instant. Re-planning today is a duplicate; next week is not.
        spec.scheduled_for.date().isoformat(),
        str(spec.amount_paise),
        str(spec.discount_paise),
    ])
    return "idem_" + hashlib.blake2b(material.encode(), digest_size=16).hexdigest()


class Dispatcher:
    def __init__(
        self, conn: psycopg.Connection, adapters: Adapters,
        rds: redis.Redis | None = None,
    ) -> None:
        self.conn = conn
        self.adapters = adapters
        self.redis = rds if rds is not None else redis.from_url(settings().redis_url)

    # -- locking -------------------------------------------------------------

    def _acquire(self, key: str) -> bool:
        try:
            return bool(self.redis.set(f"{LOCK_KEY_PREFIX}{key}", "1",
                                       nx=True, ex=LOCK_TTL_SECONDS))
        except redis.RedisError:
            # Redis is an optimisation here, not the guarantee. If it is
            # unavailable we proceed and let the unique index do its job —
            # refusing to dispatch because a cache is down would stop recovery
            # for a reason that has nothing to do with correctness.
            return True

    def _release(self, key: str) -> None:
        try:
            self.redis.delete(f"{LOCK_KEY_PREFIX}{key}")
        except redis.RedisError:
            pass

    # -- dispatch ------------------------------------------------------------

    def dispatch(self, spec: ActionSpec, decision_id: str, trace_id: str) -> DispatchOutcome:
        """Execute one decided action, exactly once."""
        key = spec.idempotency_key or fingerprint(spec)

        if not self._acquire(key):
            return DispatchOutcome(None, dispatched=False, duplicate=True,
                                   reason="another dispatcher holds this action")

        try:
            aid = self._record_intent(spec, decision_id, trace_id, key)
            if aid is None:
                return DispatchOutcome(None, dispatched=False, duplicate=True,
                                       reason="idempotency key already dispatched")

            # The intent is durable before anything external happens. A crash
            # from here on leaves a `pending` row that a sweeper can finish.
            try:
                outcome = invoke(spec, self.adapters)
            except AdapterError as exc:
                self._mark(aid, "failed", str(exc))
                self.conn.commit()
                return DispatchOutcome(aid, dispatched=False, failed=True,
                                       reason=str(exc))
            except (ValueError, KeyError) as exc:
                # A malformed action — a silent retry on a customer-initiated
                # rail, an unroutable channel. Not retryable: the same input
                # fails identically forever, so it is marked and left alone
                # rather than blocking a sweeper on every pass.
                self._mark(aid, "rejected", str(exc))
                self.conn.commit()
                return DispatchOutcome(aid, dispatched=False, failed=True,
                                       reason=str(exc))
            except Exception as exc:  # noqa: BLE001 — deliberate, see below
                # An adapter that raises something unexpected must not take the
                # rest of the batch with it. A planning cycle covers thousands
                # of cases; letting one unrecognised exception escape aborts
                # every case after it, and the merchant loses a day of recovery
                # because one HTTP client raised a type this layer had not been
                # taught about. The chaos suite found exactly that: a raw
                # ConnectionError propagated out of `plan_cycle` and ended it.
                #
                # Broad, but not silent — and that distinction is the whole
                # justification. The exception TYPE is recorded, so a
                # programming error surfaces as thousands of identical failure
                # reasons rather than disappearing. It is also logged at
                # exception level with a stack trace, and the case stays open
                # and workable tomorrow.
                log.exception(
                    "unexpected %s dispatching %s for case %s",
                    type(exc).__name__, spec.action_kind.value, spec.case_id,
                )
                self._mark(aid, "failed", f"{type(exc).__name__}: {exc}")
                self.conn.commit()
                return DispatchOutcome(aid, dispatched=False, failed=True,
                                       reason=f"{type(exc).__name__}: {exc}")

            self._mark(aid, "dispatched" if outcome.executed else "skipped")
            self._publish(spec, aid, decision_id, trace_id, outcome)
            audit.append(
                self.conn, trace_id=trace_id, merchant_id=spec.merchant_id,
                action="action.dispatched", actor=ActorKind.AGENT, subject_id=aid,
                detail={
                    "case_id": spec.case_id, "decision_id": decision_id,
                    "action_kind": spec.action_kind.value,
                    "channel": spec.channel.value,
                    "amount_paise": spec.amount_paise,
                    "discount_paise": spec.discount_paise,
                    "idempotency_key": key,
                    "external_id": outcome.external_id,
                    "vendor_replay": outcome.replayed,
                },
            )
            self.conn.commit()
            return DispatchOutcome(aid, dispatched=outcome.executed,
                                   tool_outcome=outcome)
        finally:
            self._release(key)

    # -- persistence ---------------------------------------------------------

    def _record_intent(
        self, spec: ActionSpec, decision_id: str, trace_id: str, key: str
    ) -> str | None:
        """Claim the idempotency key. Returns None if it was already claimed.

        `ON CONFLICT DO NOTHING` on the unique index rather than a prior SELECT.
        A check-then-insert has a window between the two in which another
        planner can insert, and that window is exactly where a double charge
        comes from.
        """
        aid = action_id()
        row = self.conn.execute(
            """
            INSERT INTO recovery_action
                (id, decision_id, case_id, kind, channel, idempotency_key,
                 scheduled_for, status, cost_paise, discount_paise, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (aid, decision_id, spec.case_id, spec.action_kind.value,
             spec.channel.value, key, spec.scheduled_for,
             spec.channel_cost_paise, spec.discount_paise,
             json.dumps({"discount_pct": spec.discount_pct,
                         "rail": spec.rail, "issuer": spec.issuer,
                         "dlt_template_id": spec.dlt_template_id,
                         "trace_id": trace_id}, default=str)),
        ).fetchone()
        if row is None:
            return None
        # Committed before the external call, deliberately — see the module note.
        self.conn.commit()
        return aid

    def _mark(self, aid: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE recovery_action "
            "   SET status = %s, "
            "       dispatched_at = CASE WHEN %s = 'dispatched' THEN %s "
            "                            ELSE dispatched_at END, "
            "       payload = payload || %s::jsonb "
            " WHERE id = %s",
            (status, status, datetime.now(UTC),
             json.dumps({"error": error} if error else {}), aid),
        )

    def _publish(
        self, spec: ActionSpec, aid: str, decision_id: str, trace_id: str,
        outcome: ToolOutcome,
    ) -> None:
        """Announce the action through the outbox, in this transaction.

        Not a direct Kafka produce. The action row and its announcement commit
        together or not at all; publishing directly would be a dual write, and
        a crash between them would either lose the event or announce an action
        that rolled back.
        """
        enqueue(self.conn, settings().topic_actions, spec.merchant_id, {
            "event_type": "recovery.action.dispatched",
            "action_id": aid, "decision_id": decision_id, "trace_id": trace_id,
            "case_id": spec.case_id, "obligation_id": spec.obligation_id,
            "merchant_id": spec.merchant_id, "customer_id": spec.customer_id,
            "action_kind": spec.action_kind.value, "channel": spec.channel.value,
            "amount_paise": spec.amount_paise,
            "discount_paise": spec.discount_paise,
            "cost_paise": spec.channel_cost_paise,
            "external_id": outcome.external_id,
            "scheduled_for": spec.scheduled_for,
            "dispatched_at": datetime.now(UTC),
        })


def pending_actions(conn: psycopg.Connection, older_than_minutes: int = 5) -> list[dict]:
    """Actions whose intent committed but whose outcome was never recorded.

    These are the crash survivors. Retrying one is safe precisely because the
    idempotency key is already claimed and the vendor holds it too: the retry
    either completes the original action or gets the vendor's replay response.
    """
    return conn.execute(
        "SELECT id, decision_id, case_id, kind, channel, idempotency_key, "
        "       scheduled_for, discount_paise, payload "
        "  FROM recovery_action "
        " WHERE status = 'pending' "
        "   AND created_at < now() - make_interval(mins => %s) "
        " ORDER BY created_at",
        (older_than_minutes,),
    ).fetchall()


def suppression_spec(
    case_id: str, obligation_id: str, merchant_id: str, customer_id: str,
    amount_paise: int, scheduled_for: datetime,
) -> ActionSpec:
    """A no-contact decision, recorded as a real action.

    Suppression goes through the same path as everything else — same
    fingerprint, same ledger, same audit row — so "money we chose not to chase"
    is measured by the same machinery as money we did chase, rather than being
    inferred from an absence.
    """
    from dataclasses import replace

    from yukti.domain.enums import Channel

    spec = ActionSpec(
        case_id=case_id, obligation_id=obligation_id, merchant_id=merchant_id,
        customer_id=customer_id, action_kind=ActionKind.SUPPRESS,
        channel=Channel.NONE, amount_paise=amount_paise,
        scheduled_for=scheduled_for, idempotency_key="",
    )
    return replace(spec, idempotency_key=fingerprint(spec))
