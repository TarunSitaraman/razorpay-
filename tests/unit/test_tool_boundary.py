"""The tool layer is a security boundary. These tests state what it excludes.

The claim being defended is not "refunds are blocked" but "refunds cannot be
named". A test that asserted a refund is denied would be testing a deny-list,
which is the design this deliberately does not use.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from yukti.dispatch import tools
from yukti.domain.enums import ActionKind, Channel

FORBIDDEN_CAPABILITIES = (
    "refund", "payout", "settlement", "mandate_cancel", "cancel_mandate",
    "transfer", "withdraw", "chargeback",
)


def test_no_forbidden_capability_exists_in_the_registry():
    names = tools.tool_names()
    for banned in FORBIDDEN_CAPABILITIES:
        assert not any(banned in n for n in names), f"{banned!r} is reachable"


def test_no_forbidden_capability_exists_in_the_action_enum():
    """Absence at the enum is what makes absence at the tool layer structural.

    If `ActionKind` ever gained a refund member, `_assert_complete` would demand
    a tool for it and someone would write one. So the enum is the real boundary
    and it is asserted here directly.
    """
    members = {a.value for a in ActionKind}
    for banned in FORBIDDEN_CAPABILITIES:
        assert not any(banned in m for m in members), f"{banned!r} is nameable"


def test_every_action_kind_has_exactly_one_tool():
    assert set(tools.TOOLS) == set(ActionKind)


def test_an_unregistered_tool_raises_rather_than_defaulting(monkeypatch):
    """A bypass must fail loudly, not fall through to some default behaviour."""
    spec = _spec(ActionKind.SUPPRESS, Channel.NONE)
    monkeypatch.setitem(tools.TOOLS, ActionKind.SUPPRESS, None)
    monkeypatch.delitem(tools.TOOLS, ActionKind.SUPPRESS)
    with pytest.raises(tools.UnknownTool):
        tools.invoke(spec, adapters=None)


def test_suppress_reaches_no_adapter():
    """Doing nothing must not be able to do something."""
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"suppress touched the adapter: {name}")

    outcome = tools.suppress(_spec(ActionKind.SUPPRESS, Channel.NONE), Exploding())
    assert outcome.executed
    assert outcome.status == "suppressed"


def test_escalate_reaches_no_adapter():
    """Escalation means we stopped, not that we did something else instead."""
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"escalate touched the adapter: {name}")

    outcome = tools.escalate(_spec(ActionKind.ESCALATE, Channel.NONE), Exploding())
    assert outcome.executed
    assert outcome.detail["awaiting"] == "merchant_approval"


def test_silent_retry_refuses_a_customer_initiated_rail():
    """There is nothing to retry silently when the customer must act."""
    spec = _spec(ActionKind.SILENT_RETRY, Channel.NONE, rail="upi_intent")
    with pytest.raises(ValueError, match="customer-initiated"):
        tools.silent_retry(spec, adapters=None)


def test_a_non_messaging_channel_cannot_carry_a_message():
    with pytest.raises(ValueError):
        tools._medium(Channel.VOICE)
    with pytest.raises(ValueError):
        tools._medium(Channel.NONE)


def test_channel_costs_match_the_generator():
    """The cost the allocator optimises against must be the cost the training
    data was generated with, or the model is priced against a different world."""
    from yukti_datagen.history import CHANNEL_COST_PAISE as generated

    assert tools.CHANNEL_COST_PAISE == generated


def test_a_discount_never_produces_a_non_positive_payable():
    """A 100% discount would otherwise create a zero-amount payment link."""
    calls: list[int] = []

    class Recording:
        class razorpay:
            @staticmethod
            def create_payment_link(*, amount_paise, **kw):
                calls.append(amount_paise)
                from yukti.dispatch.adapters import DispatchResult
                return DispatchResult("plink_1", "created", {})

            @staticmethod
            def notify_payment_link(*, link_id, medium):
                from yukti.dispatch.adapters import DispatchResult
                return DispatchResult(link_id, "notified", {})

    spec = _spec(ActionKind.DISCOUNT_OFFER, Channel.EMAIL,
                 discount_paise=250_000, amount_paise=250_000)
    tools.discount_offer(spec, Recording())
    assert calls == [1]


def _spec(kind: ActionKind, channel: Channel, **overrides) -> tools.ActionSpec:
    from dataclasses import replace
    spec = tools.ActionSpec(
        case_id="case_1", obligation_id="obl_1", merchant_id="mrc_1",
        customer_id="cus_1", action_kind=kind, channel=channel,
        amount_paise=250_000, scheduled_for=datetime(2026, 7, 20, 10, tzinfo=UTC),
        idempotency_key="idem_1",
    )
    return replace(spec, **overrides) if overrides else spec
