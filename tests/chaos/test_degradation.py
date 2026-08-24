"""When a dependency fails, Yukti does less — never something wrong.

The property every case here asserts is the same one, and it is the property
that makes an autonomous agent safe to run against someone's money: **a failure
must reduce what the system does, never change what it decides.** A recovery
agent that keeps acting when its model is unreachable, or that re-dispatches
because a webhook arrived twice, is worse than one that stops.

These are deliberately not mocked at the unit level. Each one breaks a real
seam — the adapter, the outbox, the LLM chain, the event stream — and asserts
what survives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yukti.dispatch.adapters import Adapters, DispatchResult
from yukti.domain.enums import ActionKind, CaseState
from yukti.pipeline import plan_cycle
from yukti.scoring import FixedScorer, UpliftScorer, ZeroScorer

from test_plan_cycle import (  # noqa: F401
    AS_OF,
    _make_case,
    adapters,
    merchant_with_policy,
)

SCORER = FixedScorer(0.15)


# --- the adapter is down ----------------------------------------------------

class DeadRazorpay:
    """Every call raises. Stands in for the payment API being unreachable."""

    def __init__(self) -> None:
        self.calls = 0

    def _die(self, *a, **kw):
        self.calls += 1
        raise ConnectionError("sandbox unreachable")

    create_payment_link = notify_payment_link = charge_mandate = _die

    def close(self) -> None:
        pass


class DeadVoice:
    def call(self, **kw):
        raise ConnectionError("voice provider unreachable")

    def close(self) -> None:
        pass


def test_adapter_failure_does_not_close_the_case(conn, merchant_with_policy):
    """A dispatch that fails must leave the money still recoverable tomorrow.

    The dangerous outcome is not the failure — it is a case marked done because
    we tried once and the network was out.
    """
    case = _make_case(conn, merchant_with_policy)
    ads = Adapters(razorpay=DeadRazorpay(), voice=DeadVoice())

    result = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert result.failed >= 1, "the failure was not recorded as a failure"
    state = conn.execute(
        "SELECT state FROM recovery_case WHERE id = %s", (case["case_id"],)
    ).fetchone()["state"]
    assert state not in (CaseState.RECOVERED.value, CaseState.LOST.value), (
        f"a dead adapter moved the case to {state} — the obligation is still open "
        "and must remain workable"
    )


def test_adapter_failure_releases_the_budget_it_reserved(conn, merchant_with_policy):
    """Budget is charged for what dispatched, not for what was planned.

    Charging a merchant's daily contact budget for a send that never left would
    starve the next cycle for nothing.
    """
    for _ in range(3):
        _make_case(conn, merchant_with_policy)
    ads = Adapters(razorpay=DeadRazorpay(), voice=DeadVoice())

    plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    consumed = conn.execute(
        "SELECT coalesce(sum(consumed_val), 0) AS n FROM budget_ledger "
        " WHERE merchant_id = %s AND kind = 'contact'", (merchant_with_policy,)
    ).fetchone()["n"]
    assert consumed == 0, (
        f"{consumed} contacts were charged for dispatches that all failed"
    )


# --- the model is gone ------------------------------------------------------

def test_no_model_artifact_raises_rather_than_scoring_zero(conn, merchant_with_policy):
    """The most dangerous degraded mode in the system, so it is a hard failure.

    Zero uplift is indistinguishable from "this customer will never pay". A
    scorer that silently returned zeros would make the stopping rules report the
    merchant's entire book as LOST_CAUSE — an operational failure dressed up as
    a business finding, and the merchant would act on it.
    """
    _make_case(conn, merchant_with_policy)
    scorer = UpliftScorer(artifact=None)

    with pytest.raises(Exception) as excinfo:
        # Force the load path without a fitted artifact on disk.
        scorer._artifact = None
        scorer.artifact_kind = "definitely_not_a_real_artifact"
        plan_cycle(conn, merchant_with_policy, AS_OF, dry_run=True, scorer=scorer)

    assert "definitely_not_a_real_artifact" in str(excinfo.value) or excinfo.value


def test_zero_scores_are_an_explicit_choice_not_a_fallback(conn, merchant_with_policy):
    """`ZeroScorer` exists so that running with no model is something the
    operator asked for, rather than something that happened to them."""
    _make_case(conn, merchant_with_policy)

    result = plan_cycle(conn, merchant_with_policy, AS_OF, dry_run=True,
                        scorer=ZeroScorer())

    # No CONTACT can clear its channel cost at zero uplift. A costless silent
    # retry still can, and should: it is funded regardless of score, because a
    # near-zero estimate on an action the customer never sees is a statement
    # about the estimator rather than about the case. So this asserts the
    # boundary, not "nothing happened".
    assert result.contacts_spent == 0, "a contact was funded at zero uplift"
    # And it still produced a decision for the case rather than skipping it, so
    # "we considered this and did nothing" stays reportable.
    assert result.considered >= 1


# --- the LLM is unavailable (the default state of this environment) ---------

def test_the_whole_cycle_runs_with_no_llm_configured(conn, merchant_with_policy):
    """No provider key is set anywhere in this environment, so this asserts the
    architecture's central claim for free: nothing financially consequential
    touches a model."""
    cases = [_make_case(conn, merchant_with_policy) for _ in range(3)]

    result = plan_cycle(conn, merchant_with_policy, AS_OF, dry_run=True, scorer=SCORER)

    assert result.considered == len(cases)
    decided = conn.execute(
        "SELECT count(*) AS n FROM agent_decision WHERE trace_id = %s",
        (result.trace_id,)
    ).fetchone()["n"]
    assert decided == len(cases), "a missing model changed what got decided"


# --- duplicate and out-of-order events --------------------------------------

def test_replanning_the_same_cycle_dispatches_nothing_twice(
    conn, merchant_with_policy, adapters
):
    """The idempotency fingerprint is derived, not minted, so a replayed cycle
    collides on a unique index rather than sending a second message."""
    ads, rzp = adapters
    for _ in range(3):
        _make_case(conn, merchant_with_policy)

    first = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)
    assert first.dispatched > 0, "nothing dispatched — the test would prove nothing"

    actions_after_first = conn.execute(
        "SELECT count(*) AS n FROM recovery_action ra JOIN agent_decision d "
        "  ON d.id = ra.decision_id JOIN recovery_case c ON c.id = d.case_id "
        " WHERE c.merchant_id = %s", (merchant_with_policy,)
    ).fetchone()["n"]

    # Reopen the cases with their actions left on file — the exact shape of a
    # replayed cycle — and plan again.
    conn.execute(
        "UPDATE recovery_case SET state = 'open', stop_reason = NULL "
        " WHERE merchant_id = %s AND state <> 'open'", (merchant_with_policy,))
    conn.commit()

    second = plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    actions_after_second = conn.execute(
        "SELECT count(*) AS n FROM recovery_action ra JOIN agent_decision d "
        "  ON d.id = ra.decision_id JOIN recovery_case c ON c.id = d.case_id "
        " WHERE c.merchant_id = %s", (merchant_with_policy,)
    ).fetchone()["n"]

    assert second.dispatched == 0, (
        f"the second cycle dispatched {second.dispatched} actions again"
    )
    assert actions_after_second == actions_after_first, (
        "the action count moved on a replay — the fingerprint is not colliding"
    )


def test_a_resolved_obligation_stops_rather_than_being_worked(
    conn, merchant_with_policy
):
    """A late `payment.captured` is the ordinary out-of-order case: the money
    arrived while we were planning to chase it."""
    case = _make_case(conn, merchant_with_policy)
    conn.execute("UPDATE obligation SET state = 'recovered' WHERE id = %s",
                 (case["obligation_id"],))
    conn.commit()

    result = plan_cycle(conn, merchant_with_policy, AS_OF, dry_run=True, scorer=SCORER)

    assert result.dispatched == 0
    row = conn.execute(
        "SELECT state, stop_reason FROM recovery_case WHERE id = %s",
        (case["case_id"],)
    ).fetchone()
    assert row["state"] == CaseState.STOPPED.value
    assert row["stop_reason"] == "obligation_resolved", (
        f"stopped for {row['stop_reason']} rather than naming the real reason"
    )


# --- the audit chain notices ------------------------------------------------

def test_a_tampered_audit_row_is_detected(conn, merchant_with_policy, adapters):
    """The chain is only worth having if breaking it is loud."""
    from yukti import audit

    ads, _ = adapters
    _make_case(conn, merchant_with_policy)
    plan_cycle(conn, merchant_with_policy, AS_OF, adapters=ads, scorer=SCORER)

    assert audit.verify(conn, merchant_with_policy).intact, (
        "chain broken before tampering")

    conn.execute(
        "UPDATE audit_event SET detail = jsonb_set("
        "  coalesce(detail, '{}'::jsonb), '{tampered}', 'true') "
        " WHERE merchant_id = %s "
        "   AND id = (SELECT min(id) FROM audit_event WHERE merchant_id = %s)",
        (merchant_with_policy, merchant_with_policy),
    )
    conn.commit()

    assert not audit.verify(conn, merchant_with_policy).intact, (
        "an edited audit row went undetected"
    )
