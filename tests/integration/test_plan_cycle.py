"""End-to-end: a decision flowing through every component in the spine.

Each component has its own tests. This asserts the properties that only exist
once they are joined — which is where the expensive bugs live, and which nothing
tested before today.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yukti import audit
from yukti.allocator import budget
from yukti.dispatch.adapters import Adapters, DispatchResult
from yukti.domain.enums import ActionKind, CaseState, PolicyVerdict, StopReason
from yukti.domain.ids import case_id, customer_id, obligation_id
from yukti.pipeline import plan_cycle
from yukti.scoring import FixedScorer

AS_OF = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)

# A fixed effect, so these tests exercise the spine rather than the model. The
# real scorer needs a fitted artifact; depending on one would make the
# integration tests fail for a reason that has nothing to do with integration.
SCORER = FixedScorer(0.12)


class CountingRazorpay:
    def __init__(self) -> None:
        self.links: list[dict] = []
        self.notifies: list[dict] = []
        self.charges: list[dict] = []

    def create_payment_link(self, **kw) -> DispatchResult:
        self.links.append(kw)
        return DispatchResult(f"plink_{len(self.links)}", "created", {})

    def notify_payment_link(self, *, link_id, medium) -> DispatchResult:
        self.notifies.append({"link_id": link_id, "medium": medium})
        return DispatchResult(link_id, "notified", {})

    def charge_mandate(self, **kw) -> DispatchResult:
        self.charges.append(kw)
        return DispatchResult(f"pay_{len(self.charges)}", "captured", {})

    def call(self, **kw) -> DispatchResult:
        return DispatchResult("call_1", "completed", {})


@pytest.fixture
def adapters():
    rzp = CountingRazorpay()
    return Adapters(razorpay=rzp, voice=rzp), rzp


@pytest.fixture
def merchant_with_policy(conn, merchant):
    import json
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from yukti.domain.ids import new_id
    conn.execute(
        "INSERT INTO policy_pack (id, merchant_id, kind, version, compiled, "
        "approved_by, approved_at, active) VALUES (%s,%s,'merchant',1,%s,'test',%s,true)",
        (new_id("pol"), merchant, json.dumps({
            "max_contacts_per_customer_per_week": 3,
            "max_discount_pct": 15.0,
            "approval_threshold_paise": 25_000_00,
            "min_obligation_paise": 100_00,
            "allowed_channels": ["whatsapp", "sms", "email"],
        }), _dt.now(_UTC)),
    )
    conn.execute(
        "INSERT INTO budget_ledger (merchant_id, kind, window_start, limit_val) "
        "VALUES (%s,'contact',%s,50), (%s,'discount',%s,500000)",
        (merchant, AS_OF.date(), merchant, AS_OF.date()),
    )
    return merchant


def _make_case(
    conn, merchant, *, amount_paise=250_000, decline_code="INSUFFICIENT_FUNDS",
    rail="upi_autopay", consent=None, kind="subscription_cycle",
    opted_out=False, failed_days_ago=2, arm="treatment", promise_days=None,
) -> dict:
    import json

    from yukti.domain.ids import attempt_id
    cust = customer_id()
    conn.execute(
        "INSERT INTO customer (id, merchant_id, ltv_band, tenure_days, consent, "
        "archetype, preferred_channel, opted_out_at, prior_payments, prior_failures, "
        "prior_contacts, prior_contact_responses, prior_optouts, "
        "days_since_last_payment, prior_unprompted_payments, prior_prompted_payments) "
        "VALUES (%s,%s,'mid',400,%s,'persuadable','whatsapp',%s,8,2,3,1,0,25,5,3)",
        (cust, merchant,
         json.dumps(consent if consent is not None
                    else {"whatsapp": True, "sms": True, "email": True}),
         AS_OF if opted_out else None),
    )
    oid = obligation_id()
    failed_at = AS_OF - timedelta(days=failed_days_ago)
    conn.execute(
        "INSERT INTO obligation (id, merchant_id, customer_id, kind, amount_paise, "
        "due_at, state, version) VALUES (%s,%s,%s,%s,%s,%s,'open',1)",
        (oid, merchant, cust, kind, amount_paise, failed_at),
    )
    conn.execute(
        "INSERT INTO payment_attempt (id, obligation_id, rail, issuer, psp, status, "
        "decline_code, amount_paise, attempted_at) "
        "VALUES (%s,%s,%s,'HDFC','RZP','failed',%s,%s,%s)",
        (attempt_id(), oid, rail, decline_code, amount_paise, failed_at),
    )
    if promise_days is not None:
        from yukti.domain.ids import promise_id
        conn.execute(
            "INSERT INTO promise_to_pay (id, obligation_id, promised_amount_paise, "
            "promised_for, source, state, confidence, created_at) "
            "VALUES (%s,%s,%s,%s,'customer_reply','open',0.8,%s)",
            (promise_id(), oid, amount_paise,
             (AS_OF + timedelta(days=promise_days)).date(), failed_at),
        )
    cid = case_id()
    conn.execute(
        "INSERT INTO recovery_case (id, obligation_id, merchant_id, customer_id, "
        "state, arm, opened_at) VALUES (%s,%s,%s,%s,'open',%s,%s)",
        (cid, oid, merchant, cust, arm, failed_at),
    )
    return {"case_id": cid, "obligation_id": oid, "customer_id": cust}


# --- the spine runs ---------------------------------------------------------

def test_a_decision_flows_end_to_end(conn, merchant_with_policy, adapters):
    ads, rzp = adapters
    _make_case(conn, merchant_with_policy)

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert result.considered == 1
    assert result.stopped + result.dispatched + result.suppressed + \
           result.escalated + result.blocked == 1
    rows = conn.execute(
        "SELECT count(*) AS n FROM agent_decision WHERE trace_id = %s", (result.trace_id,)
    ).fetchone()["n"]
    assert rows == 1, "every considered case must leave a decision row"


def test_no_policy_violation_is_ever_dispatched(conn, merchant_with_policy, adapters):
    """The property the track brief calls compliant escalation.

    Checked against what actually reached an adapter, not against what the
    engine said, so a bug that dispatched despite a BLOCK would be caught.
    """
    ads, _ = adapters
    for _ in range(12):
        _make_case(conn, merchant_with_policy)

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert result.blocked == 0, "an action was blocked AFTER allocation — the " \
                                "feasibility filter and the full check disagreed"
    dispatched_blocks = conn.execute(
        """
        SELECT count(*) AS n
          FROM recovery_action a
          JOIN agent_decision d ON d.id = a.decision_id
         WHERE d.trace_id = %s AND d.policy_verdict = 'block'
        """,
        (result.trace_id,),
    ).fetchone()["n"]
    assert dispatched_blocks == 0


def test_budgets_are_never_exceeded(conn, merchant_with_policy, adapters):
    ads, _ = adapters
    for _ in range(40):
        _make_case(conn, merchant_with_policy, amount_paise=500_000)

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    contact = budget.load(conn, merchant_with_policy, "contact", AS_OF.date())
    discount = budget.load(conn, merchant_with_policy, "discount", AS_OF.date())
    assert contact.consumed_val <= contact.limit_val
    assert discount.consumed_val <= discount.limit_val
    assert result.contacts_spent <= 50


def test_running_twice_dispatches_nothing_new(conn, merchant_with_policy, adapters):
    """Idempotency through the ledger, demonstrated on the real path.

    The cases are deliberately reset to `open` between runs, so the second cycle
    re-plans them in full and the collision has to be caught by the fingerprint
    rather than by a state check that skipped the work.
    """
    ads, rzp = adapters
    for _ in range(8):
        _make_case(conn, merchant_with_policy)

    first = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)
    calls_after_first = len(rzp.links) + len(rzp.charges)

    conn.execute(
        "UPDATE recovery_case SET state = 'open', stop_reason = NULL "
        " WHERE merchant_id = %s AND state <> 'open'", (merchant_with_policy,))
    conn.commit()

    second = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert second.dispatched == 0, "the second cycle dispatched again"
    assert second.duplicates >= 1 or first.dispatched == 0
    assert len(rzp.links) + len(rzp.charges) == calls_after_first


# --- stopping rules ---------------------------------------------------------

def test_an_open_promise_stops_the_chase(conn, merchant_with_policy, adapters):
    """Demo beat 7, and the rule that was dead code until today."""
    ads, rzp = adapters
    _make_case(conn, merchant_with_policy, kind="invoice", promise_days=5,
               amount_paise=800_000)

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert result.stop_breakdown.get(StopReason.OPEN_PROMISE_TO_PAY.value) == 1
    assert not rzp.links and not rzp.charges, "we chased through a promise"


def test_an_opted_out_customer_is_never_contacted(conn, merchant_with_policy, adapters):
    ads, rzp = adapters
    _make_case(conn, merchant_with_policy, opted_out=True)

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert result.stop_breakdown.get(StopReason.CUSTOMER_OPTED_OUT.value) == 1
    assert not rzp.notifies


def test_a_permanent_decline_is_a_lost_cause(conn, merchant_with_policy, adapters):
    ads, _ = adapters
    _make_case(conn, merchant_with_policy, decline_code="MANDATE_REVOKED")

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert result.stop_breakdown.get(StopReason.LOST_CAUSE.value) == 1


def test_every_stop_carries_a_named_rule(conn, merchant_with_policy, adapters):
    ads, _ = adapters
    _make_case(conn, merchant_with_policy, opted_out=True)
    _make_case(conn, merchant_with_policy, decline_code="MANDATE_REVOKED")
    _make_case(conn, merchant_with_policy, failed_days_ago=40)

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    unnamed = conn.execute(
        "SELECT count(*) AS n FROM recovery_case "
        " WHERE merchant_id = %s AND state = 'stopped' AND stop_reason IS NULL",
        (merchant_with_policy,),
    ).fetchone()["n"]
    assert unnamed == 0
    assert sum(result.stop_breakdown.values()) == result.stopped


# --- escalation and holdout -------------------------------------------------

def test_a_large_obligation_escalates_instead_of_acting(conn, merchant_with_policy,
                                                        adapters):
    """Above the approval threshold the agent proposes; a human decides."""
    ads, rzp = adapters
    _make_case(conn, merchant_with_policy, amount_paise=48_000_00,
               decline_code="INSUFFICIENT_FUNDS", rail="upi_collect")

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert result.escalated == 1
    assert not rzp.links, "an above-threshold action was executed"
    state = conn.execute(
        "SELECT state FROM recovery_case WHERE merchant_id = %s", (merchant_with_policy,)
    ).fetchone()["state"]
    assert state == CaseState.ESCALATED.value


def test_a_holdout_case_is_planned_but_never_acted_on(conn, merchant_with_policy,
                                                      adapters):
    """The denominator has to be observed, not assumed."""
    ads, rzp = adapters
    _make_case(conn, merchant_with_policy, arm="holdout")

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert not rzp.links and not rzp.charges and not rzp.notifies
    decisions = conn.execute(
        "SELECT count(*) AS n FROM agent_decision WHERE trace_id = %s",
        (result.trace_id,),
    ).fetchone()["n"]
    assert decisions == 1, "a holdout case must still be reasoned about"
    actions = conn.execute(
        "SELECT count(*) AS n FROM recovery_action a "
        "  JOIN agent_decision d ON d.id = a.decision_id WHERE d.trace_id = %s",
        (result.trace_id,),
    ).fetchone()["n"]
    assert actions == 0


# --- audit ------------------------------------------------------------------

def test_the_audit_chain_verifies_after_a_cycle(conn, merchant_with_policy, adapters):
    ads, _ = adapters
    for _ in range(6):
        _make_case(conn, merchant_with_policy)
    _make_case(conn, merchant_with_policy, opted_out=True)

    plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert audit.verify(conn, merchant_with_policy).intact


def test_a_cycle_is_bracketed_in_the_audit_trail(conn, merchant_with_policy, adapters):
    ads, _ = adapters
    _make_case(conn, merchant_with_policy)
    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    actions = [
        r["action"] for r in conn.execute(
            "SELECT action FROM audit_event WHERE trace_id = %s ORDER BY id",
            (result.trace_id,),
        )
    ]
    assert actions[0] == "plan_cycle.started"
    assert actions[-1] == "plan_cycle.completed"


def test_dry_run_decides_without_touching_an_adapter(conn, merchant_with_policy,
                                                     adapters):
    ads, rzp = adapters
    for _ in range(5):
        _make_case(conn, merchant_with_policy)

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, dry_run=True,
                          scorer=SCORER)

    assert not rzp.links and not rzp.charges
    assert result.considered == 5
    decisions = conn.execute(
        "SELECT count(*) AS n FROM agent_decision WHERE trace_id = %s",
        (result.trace_id,),
    ).fetchone()["n"]
    assert decisions == 5


def test_an_empty_merchant_completes_cleanly(conn, merchant_with_policy, adapters):
    ads, _ = adapters
    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)
    assert result.considered == 0
    assert result.dispatched == 0
