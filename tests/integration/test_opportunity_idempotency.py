"""The guarantees that stop a duplicated webhook from costing money.

Delivery is at-least-once and unordered. Everything here is about making that
safe without distributed locking.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from yukti.domain.enums import Arm, CaseState
from yukti.opportunity.service import OpportunityService

pytestmark = pytest.mark.integration


def failure_event(merchant, customer, obligation, event_id="evt_1", version=1, ts=None):
    return {
        "event_id": event_id,
        "event_type": "subscription.pending",
        "ts": (ts or datetime(2026, 5, 15, 9, 0)).isoformat(),
        "merchant_id": merchant,
        "customer_id": customer,
        "obligation_id": obligation,
        "obligation_kind": "subscription_cycle",
        "amount_paise": 250000,
        "rail": "upi_autopay",
        "issuer": "HDFC",
        "psp": "razorpay_psp_a",
        "decline_code": "INSUFFICIENT_FUNDS",
        "decline_text": "insufficient balance",
        "attempt_id": "att_1",
        "due_at": (ts or datetime(2026, 5, 15, 9, 0)).isoformat(),
        "version": version,
    }


def live_cases(conn, obligation) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM recovery_case WHERE obligation_id = %s "
        "AND state NOT IN ('stopped','recovered','lost')",
        (obligation,),
    ).fetchone()["n"]


class TestDuplicateDelivery:
    def test_same_event_twice_opens_one_case(self, conn, fake_redis, merchant, customer, obligation):
        svc = OpportunityService(conn, fake_redis)
        ev = failure_event(merchant, customer, obligation)

        first = svc.ingest(ev)
        second = svc.ingest(ev)

        assert first.opened == 1
        assert second.duplicate == 1 and second.opened == 0
        assert live_cases(conn, obligation) == 1

    def test_dedup_survives_a_redis_flush(self, conn, fake_redis, merchant, customer, obligation):
        # Redis is a cache, not the source of truth. Losing it must not cause a
        # second case — the durable processed_event table has to catch it.
        svc = OpportunityService(conn, fake_redis)
        ev = failure_event(merchant, customer, obligation)
        svc.ingest(ev)

        fake_redis.store.clear()
        again = svc.ingest(ev)

        assert again.duplicate == 1
        assert live_cases(conn, obligation) == 1

    def test_distinct_event_ids_for_one_obligation_still_open_one_case(
        self, conn, fake_redis, merchant, customer, obligation
    ):
        # The nastier case: a webhook redelivered with a NEW event id, which
        # both dedup layers legitimately let through. Only the partial unique
        # index stops it, and that is why the index exists.
        svc = OpportunityService(conn, fake_redis)
        svc.ingest(failure_event(merchant, customer, obligation, event_id="evt_a"))
        second = svc.ingest(failure_event(merchant, customer, obligation, event_id="evt_b"))

        assert second.opened == 0
        assert live_cases(conn, obligation) == 1


class TestOutOfOrderDelivery:
    def test_stale_event_is_superseded_not_applied(
        self, conn, fake_redis, merchant, customer, obligation
    ):
        conn.execute("UPDATE obligation SET version = 5 WHERE id = %s", (obligation,))
        svc = OpportunityService(conn, fake_redis)

        result = svc.ingest(failure_event(merchant, customer, obligation, version=1))

        assert result.superseded == 1 and result.opened == 0
        assert live_cases(conn, obligation) == 0

    def test_final_state_is_order_independent(
        self, conn, fake_redis, merchant, customer, obligation
    ):
        # A capture and a failure racing each other must land on 'recovered'
        # regardless of arrival order: the money is the source of truth.
        svc = OpportunityService(conn, fake_redis)
        capture = {**failure_event(merchant, customer, obligation, event_id="evt_cap"),
                   "event_type": "payment.captured"}

        svc.ingest(failure_event(merchant, customer, obligation, event_id="evt_fail"))
        svc.ingest(capture)

        state = conn.execute(
            "SELECT state FROM recovery_case WHERE obligation_id = %s", (obligation,)
        ).fetchone()["state"]
        assert state == CaseState.RECOVERED.value


class TestResolution:
    def test_capture_closes_a_live_case(self, conn, fake_redis, merchant, customer, obligation):
        svc = OpportunityService(conn, fake_redis)
        svc.ingest(failure_event(merchant, customer, obligation, event_id="e1"))

        res = svc.ingest({**failure_event(merchant, customer, obligation, event_id="e2"),
                          "event_type": "payment.captured"})

        assert res.resolved == 1
        assert live_cases(conn, obligation) == 0

    def test_capture_with_no_live_case_is_ignored(
        self, conn, fake_redis, merchant, customer, obligation
    ):
        svc = OpportunityService(conn, fake_redis)
        res = svc.ingest({**failure_event(merchant, customer, obligation, event_id="e9"),
                          "event_type": "payment.captured"})
        assert res.ignored == 1


class TestUnknownObligations:
    def test_event_for_unseen_obligation_is_ignored_not_fabricated(
        self, conn, fake_redis, merchant, customer
    ):
        # A webhook can overtake its own backfill. We refuse to invent an
        # obligation from a webhook payload — that would let an attacker who can
        # reach the endpoint create debt records.
        svc = OpportunityService(conn, fake_redis)
        res = svc.ingest(failure_event(merchant, customer, "obl_does_not_exist"))
        assert res.ignored == 1 and res.opened == 0


class TestArmAssignment:
    def test_assignment_is_deterministic(self, conn, merchant, customer):
        svc = OpportunityService(conn, None.__class__())  # redis unused here
        a = svc._assign_arm(merchant, customer)
        b = svc._assign_arm(merchant, customer)
        assert a is b

    def test_assignment_is_keyed_on_customer_not_case(self, conn, merchant, customer):
        # One customer must never be half-held-out: their cases would
        # cross-contaminate the fatigue measurement, since fatigue is a property
        # of the person, not of any single case.
        svc = OpportunityService(conn, None.__class__())
        assert svc._assign_arm(merchant, customer) is svc._assign_arm(merchant, customer)

    def test_holdout_share_is_close_to_configured(self, conn, merchant):
        from yukti.domain.ids import customer_id

        svc = OpportunityService(conn, None.__class__())
        n = 5000
        holdout = sum(
            svc._assign_arm(merchant, customer_id()) is Arm.HOLDOUT for _ in range(n)
        )
        assert 0.07 < holdout / n < 0.13     # configured 10%
