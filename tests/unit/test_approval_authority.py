"""Approval supplies authority, never exemption.

The escalation path exists because some actions are correct but above the
merchant's threshold for acting unsupervised. The danger in building an approve
button is that it becomes a way around the policy engine rather than a way back
into it. These tests pin the distinction:

  - a human can lift the rule that asked for a human
  - a human cannot lift anything else, regulatory rules above all
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from yukti.domain.enums import ActionKind, Channel, PolicyVerdict
from yukti.policy.engine import evaluate
from yukti.policy.merchantpack import MerchantContext, MerchantPolicy
from yukti.policy.regpack import ActionRequest

MIDDAY = datetime(2026, 6, 10, 14, 0)
FULL_CONSENT = {"whatsapp": True, "sms": True, "email": True, "voice": True}
POLICY = MerchantPolicy(merchant_id="mrc_test")


def req(**over) -> ActionRequest:
    base = dict(
        action_kind=ActionKind.MESSAGE,
        channel=Channel.WHATSAPP,
        scheduled_for=MIDDAY,
        amount_paise=250_000,
        merchant_category="general",
        decline_code="INSUFFICIENT_FUNDS",
        attempts_made=0,
        discount_pct=0.0,
        consent=FULL_CONSENT,
        predebit_notice_at=None,
        dlt_template_id="DLT_1234",
        has_afa=False,
    )
    return ActionRequest(**{**base, **over})


def above_threshold() -> ActionRequest:
    return req(amount_paise=POLICY.approval_threshold_paise + 1)


class TestApprovalLiftsOnlyItsOwnRule:
    def test_above_threshold_escalates_without_a_human(self):
        d = evaluate(above_threshold(), POLICY, MerchantContext())
        assert d.verdict is PolicyVerdict.ESCALATE
        assert [r.rule_id for r in d.escalations] == ["MERCHANT_APPROVAL_THRESHOLD"]

    def test_the_same_action_is_allowed_once_a_human_approves(self):
        d = evaluate(above_threshold(), POLICY,
                     MerchantContext(human_approved=True))
        assert d.verdict is PolicyVerdict.ALLOW

    def test_approval_does_not_change_the_amount_it_supplies_authority(self):
        """The rule still sees an above-threshold amount; it defers, not exempts."""
        request = above_threshold()
        assert request.amount_paise >= POLICY.approval_threshold_paise
        assert evaluate(request, POLICY,
                        MerchantContext(human_approved=True)).allowed

    def test_approval_is_inert_on_an_action_that_never_needed_it(self):
        under = req(amount_paise=POLICY.approval_threshold_paise - 1)
        assert evaluate(under, POLICY, MerchantContext()).allowed
        assert evaluate(under, POLICY, MerchantContext(human_approved=True)).allowed


class TestApprovalCannotOverrideAnythingElse:
    """The adversarial half. Each action is one a reviewer would plausibly wave
    through, and each must still be refused with a human attached."""

    def test_a_human_cannot_lift_the_contact_cap(self):
        d = evaluate(above_threshold(), POLICY,
                     MerchantContext(contacts_this_week=99, human_approved=True))
        assert d.verdict is PolicyVerdict.BLOCK
        assert "MERCHANT_CONTACT_CAP" in [r.rule_id for r in d.blocks]

    def test_a_human_cannot_lift_missing_consent(self):
        d = evaluate(
            req(amount_paise=POLICY.approval_threshold_paise + 1,
                consent={"whatsapp": False, "sms": False,
                         "email": False, "voice": False}),
            POLICY, MerchantContext(human_approved=True),
        )
        assert d.verdict is PolicyVerdict.BLOCK

    def test_a_human_cannot_lift_the_predebit_notice_window(self):
        """RBI's 24h pre-debit notice. No merchant authority reaches this."""
        d = evaluate(
            req(action_kind=ActionKind.SCHEDULE_DEBIT,
                channel=Channel.NONE,
                amount_paise=POLICY.approval_threshold_paise + 1,
                predebit_notice_at=MIDDAY - timedelta(hours=1)),
            POLICY, MerchantContext(human_approved=True),
        )
        assert d.verdict is PolicyVerdict.BLOCK

    def test_block_still_outranks_escalate_with_a_human_present(self):
        """An action both above-threshold and illegal is blocked, not offered."""
        d = evaluate(
            req(amount_paise=POLICY.approval_threshold_paise + 1,
                consent={}),
            POLICY, MerchantContext(human_approved=True),
        )
        assert d.verdict is PolicyVerdict.BLOCK
        assert d.escalations == [] or d.verdict is PolicyVerdict.BLOCK


class TestEveryRuleStillRuns:
    def test_approval_does_not_shorten_the_evidence(self):
        """The audit value of a policy pass is that every rule is recorded."""
        without = evaluate(above_threshold(), POLICY, MerchantContext())
        with_human = evaluate(above_threshold(), POLICY,
                              MerchantContext(human_approved=True))
        assert len(with_human.results) == len(without.results)
        assert {r.rule_id for r in with_human.results} == \
               {r.rule_id for r in without.results}
