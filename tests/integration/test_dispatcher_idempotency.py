"""Exactly-once effect, which is the property that protects money.

The claim under test is narrow and specific: dispatching the same *meaning*
twice produces one effect. Not "usually one" and not "one because the caller
was careful" — one, enforced by the database, with the cheap layers removed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yukti.dispatch.adapters import AdapterError, Adapters, DispatchResult
from yukti.dispatch.dispatcher import Dispatcher, fingerprint
from yukti.dispatch.tools import ActionSpec
from yukti.domain.enums import ActionKind, Channel
from yukti.domain.ids import case_id, decision_id, run_id, trace_id

AT = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


class RecordingRazorpay:
    """Counts calls, so a second effect is visible rather than inferred."""

    def __init__(self) -> None:
        self.links: list[dict] = []
        self.charges: list[dict] = []
        self.notifies: list[dict] = []

    def create_payment_link(self, **kw) -> DispatchResult:
        self.links.append(kw)
        return DispatchResult(f"plink_{len(self.links)}", "created", {})

    def notify_payment_link(self, *, link_id, medium) -> DispatchResult:
        self.notifies.append({"link_id": link_id, "medium": medium})
        return DispatchResult(link_id, "notified", {})

    def charge_mandate(self, **kw) -> DispatchResult:
        self.charges.append(kw)
        return DispatchResult(f"pay_{len(self.charges)}", "captured", {})


class ExplodingRazorpay(RecordingRazorpay):
    def charge_mandate(self, **kw) -> DispatchResult:
        self.charges.append(kw)
        raise AdapterError("connection reset by peer")


class NoRedis:
    """Redis unavailable.

    Used deliberately: the lock is an optimisation and the unique index is the
    guarantee, so every duplicate test here runs with the lock removed. A test
    that only passes with Redis up is testing the wrong layer.
    """

    def set(self, *a, **kw):
        import redis
        raise redis.RedisError("down")

    def delete(self, *a, **kw):
        import redis
        raise redis.RedisError("down")


@pytest.fixture
def adapters():
    rzp = RecordingRazorpay()
    return Adapters(razorpay=rzp, voice=rzp), rzp


@pytest.fixture
def case(conn, merchant, customer, obligation):
    cid = case_id()
    conn.execute(
        "INSERT INTO recovery_case (id, obligation_id, merchant_id, customer_id, "
        "state, arm, opened_at) VALUES (%s,%s,%s,%s,'open','treatment',%s)",
        (cid, obligation, merchant, customer, AT - timedelta(days=2)),
    )
    rid = run_id()
    conn.execute(
        "INSERT INTO agent_run (id, merchant_id, kind, trace_id, status) "
        "VALUES (%s,%s,'planner',%s,'running')", (rid, merchant, trace_id()))
    did = decision_id()
    conn.execute(
        "INSERT INTO agent_decision (id, run_id, case_id, trace_id, action_kind, "
        "channel, reason, policy_verdict) "
        "VALUES (%s,%s,%s,%s,'message','whatsapp','test','allow')",
        (did, rid, cid, trace_id()))
    return {"case_id": cid, "obligation_id": obligation, "merchant_id": merchant,
            "customer_id": customer, "decision_id": did}


def _spec(case: dict, **overrides) -> ActionSpec:
    from dataclasses import replace
    spec = ActionSpec(
        case_id=case["case_id"], obligation_id=case["obligation_id"],
        merchant_id=case["merchant_id"], customer_id=case["customer_id"],
        action_kind=ActionKind.MESSAGE, channel=Channel.WHATSAPP,
        amount_paise=250000, scheduled_for=AT, idempotency_key="",
    )
    spec = replace(spec, **overrides)
    return replace(spec, idempotency_key=fingerprint(spec))


# --- the fingerprint ---------------------------------------------------------

def test_fingerprint_is_derived_from_meaning_not_from_the_request(case):
    """Two independently constructed proposals with the same meaning collide."""
    assert _spec(case).idempotency_key == _spec(case).idempotency_key


def test_same_day_different_hour_is_the_same_action(case):
    morning = _spec(case, scheduled_for=AT.replace(hour=9))
    evening = _spec(case, scheduled_for=AT.replace(hour=19))
    assert morning.idempotency_key == evening.idempotency_key


def test_next_week_is_a_different_action(case):
    later = _spec(case, scheduled_for=AT + timedelta(days=7))
    assert later.idempotency_key != _spec(case).idempotency_key


def test_a_different_amount_is_a_different_action(case):
    assert _spec(case, amount_paise=250001).idempotency_key != _spec(case).idempotency_key


def test_a_different_channel_is_a_different_action(case):
    assert _spec(case, channel=Channel.SMS).idempotency_key != _spec(case).idempotency_key


# --- exactly-once ------------------------------------------------------------

def test_dispatching_twice_produces_one_effect(conn, case, adapters):
    """The core claim, with the Redis lock deliberately unavailable."""
    ads, rzp = adapters
    d = Dispatcher(conn, ads, rds=NoRedis())
    spec = _spec(case)

    first = d.dispatch(spec, case["decision_id"], trace_id())
    second = d.dispatch(spec, case["decision_id"], trace_id())

    assert first.dispatched
    assert not second.dispatched
    assert second.duplicate
    assert len(rzp.links) == 1, "the vendor was asked to act twice"
    assert len(rzp.notifies) == 1

    rows = conn.execute(
        "SELECT count(*) AS n FROM recovery_action WHERE case_id = %s",
        (case["case_id"],),
    ).fetchone()["n"]
    assert rows == 1


def test_a_re_derived_spec_also_collides(conn, case, adapters):
    """A re-run builds a fresh spec object; it must still be recognised."""
    ads, rzp = adapters
    d = Dispatcher(conn, ads, rds=NoRedis())
    d.dispatch(_spec(case), case["decision_id"], trace_id())
    again = d.dispatch(_spec(case), case["decision_id"], trace_id())
    assert again.duplicate
    assert len(rzp.links) == 1


def test_intent_survives_an_adapter_failure(conn, case, merchant, customer, obligation):
    """A failed call leaves a durable record, not a silent gap.

    This is the crash-safety property: the intent commits before the external
    call, so an action that may or may not have happened is recoverable rather
    than invisible.
    """
    rzp = ExplodingRazorpay()
    d = Dispatcher(conn, Adapters(razorpay=rzp, voice=rzp), rds=NoRedis())
    spec = _spec(case, action_kind=ActionKind.SILENT_RETRY, channel=Channel.NONE,
                 rail="upi_autopay")

    outcome = d.dispatch(spec, case["decision_id"], trace_id())

    assert outcome.failed
    row = conn.execute(
        "SELECT status, idempotency_key, payload FROM recovery_action WHERE id = %s",
        (outcome.action_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["idempotency_key"] == spec.idempotency_key
    assert "connection reset" in row["payload"]["error"]


def test_an_incoherent_action_is_rejected_not_retried(conn, case, adapters):
    """A silent retry on a customer-initiated rail cannot succeed, ever.

    Marked `rejected` rather than `failed` so a sweeper does not spend the rest
    of its life retrying something that fails identically every time.
    """
    ads, rzp = adapters
    d = Dispatcher(conn, ads, rds=NoRedis())
    spec = _spec(case, action_kind=ActionKind.SILENT_RETRY, channel=Channel.NONE,
                 rail="upi_intent")

    outcome = d.dispatch(spec, case["decision_id"], trace_id())

    assert outcome.failed
    assert len(rzp.charges) == 0
    row = conn.execute("SELECT status FROM recovery_action WHERE id = %s",
                       (outcome.action_id,)).fetchone()
    assert row["status"] == "rejected"


def test_dispatch_writes_an_outbox_row_in_the_same_transaction(conn, case, adapters):
    """No dual write: the action and its announcement commit together."""
    ads, _ = adapters
    d = Dispatcher(conn, ads, rds=NoRedis())
    outcome = d.dispatch(_spec(case), case["decision_id"], trace_id())

    row = conn.execute(
        "SELECT payload FROM outbox WHERE payload->>'action_id' = %s",
        (outcome.action_id,),
    ).fetchone()
    assert row is not None
    assert row["payload"]["event_type"] == "recovery.action.dispatched"


def test_dispatch_appends_a_verifying_audit_row(conn, case, adapters, merchant):
    from yukti import audit

    ads, _ = adapters
    d = Dispatcher(conn, ads, rds=NoRedis())
    outcome = d.dispatch(_spec(case), case["decision_id"], trace_id())

    row = conn.execute(
        "SELECT action, subject_id FROM audit_event WHERE subject_id = %s",
        (outcome.action_id,),
    ).fetchone()
    assert row["action"] == "action.dispatched"
    assert audit.verify(conn, merchant).intact


def test_the_lock_stops_a_concurrent_dispatcher(conn, case, adapters):
    """With Redis up, the second caller is turned away before touching the DB."""
    class HeldLock:
        def set(self, *a, **kw):
            return None          # SET NX on an existing key

        def delete(self, *a, **kw):
            return 1

    ads, rzp = adapters
    d = Dispatcher(conn, ads, rds=HeldLock())
    outcome = d.dispatch(_spec(case), case["decision_id"], trace_id())

    assert outcome.duplicate
    assert not rzp.links, "the adapter was reached while another dispatcher held the lock"
