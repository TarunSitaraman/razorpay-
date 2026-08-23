"""The policy engine — compliant escalation, and zero escapes.

The adversarial class at the bottom is the one that matters: every action there
is something a competent planner would genuinely propose because it is locally
optimal, and every one of them must be stopped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from yukti.domain.enums import ActionKind, Channel, PolicyVerdict
from yukti.policy.engine import all_rule_ids, evaluate, is_feasible
from yukti.policy.merchantpack import (
    MerchantContext,
    MerchantPolicy,
    compile_from_settings,
)
from yukti.policy.regpack import ActionRequest

MIDDAY = datetime(2026, 6, 10, 14, 0)
FULL_CONSENT = {"whatsapp": True, "sms": True, "email": True, "voice": True}


def req(**over) -> ActionRequest:
    """A compliant message. Tests change one thing at a time."""
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


POLICY = MerchantPolicy(merchant_id="mrc_test")


class TestBaseline:
    def test_a_compliant_action_is_allowed(self):
        assert evaluate(req(), POLICY).allowed

    def test_every_rule_runs_even_after_a_block(self):
        # A merchant fixing one problem should see the rest at the same time,
        # not rediscover them one at a time.
        d = evaluate(req(scheduled_for=MIDDAY.replace(hour=23), consent={}), POLICY)
        assert len(d.blocks) >= 2
        assert len(d.results) == len(all_rule_ids()["regulatory"]) + len(
            all_rule_ids()["merchant"]
        )


class TestRegulatory:
    def test_debit_without_predebit_notice_is_blocked(self):
        d = evaluate(req(action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE),
                     POLICY)
        assert any(r.rule_id == "RBI_PREDEBIT_24H" for r in d.blocks)

    def test_debit_inside_24h_of_notice_is_blocked(self):
        d = evaluate(req(
            action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE,
            predebit_notice_at=MIDDAY - timedelta(hours=6)), POLICY)
        assert any(r.rule_id == "RBI_PREDEBIT_24H" for r in d.blocks)

    def test_debit_after_24h_is_permitted(self):
        d = evaluate(req(
            action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE,
            predebit_notice_at=MIDDAY - timedelta(hours=25)), POLICY)
        assert not any(r.rule_id == "RBI_PREDEBIT_24H" for r in d.blocks)

    def test_above_afa_limit_is_blocked(self):
        d = evaluate(req(
            action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE,
            amount_paise=40_000_00,
            predebit_notice_at=MIDDAY - timedelta(hours=25)), POLICY)
        assert any(r.rule_id == "RBI_AFA_LIMIT" for r in d.blocks)

    def test_exempt_categories_get_the_raised_ceiling(self):
        d = evaluate(req(
            action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE,
            amount_paise=40_000_00, merchant_category="insurance",
            predebit_notice_at=MIDDAY - timedelta(hours=25)), POLICY)
        assert not any(r.rule_id == "RBI_AFA_LIMIT" for r in d.blocks)

    def test_afa_present_permits_a_large_debit(self):
        d = evaluate(req(
            action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE,
            amount_paise=40_000_00, has_afa=True,
            predebit_notice_at=MIDDAY - timedelta(hours=25)), POLICY)
        assert not any(r.rule_id == "RBI_AFA_LIMIT" for r in d.blocks)

    def test_npci_cap_uses_the_shared_decline_table(self):
        from yukti.domain.decline import lookup

        cap = lookup("AP39").max_attempts
        d = evaluate(req(
            action_kind=ActionKind.SILENT_RETRY, channel=Channel.NONE,
            decline_code="AP39", attempts_made=cap), POLICY)
        assert any(r.rule_id == "NPCI_REPRESENT_CAP" for r in d.blocks)

    @pytest.mark.parametrize("hour", [0, 5, 8, 21, 22, 23])
    def test_contact_outside_trai_hours_is_blocked(self, hour):
        d = evaluate(req(scheduled_for=MIDDAY.replace(hour=hour)), POLICY)
        assert any(r.rule_id == "TRAI_QUIET_HOURS" for r in d.blocks)

    @pytest.mark.parametrize("hour", [9, 12, 18, 20])
    def test_contact_inside_trai_hours_is_permitted(self, hour):
        d = evaluate(req(scheduled_for=MIDDAY.replace(hour=hour)), POLICY)
        assert not any(r.rule_id == "TRAI_QUIET_HOURS" for r in d.blocks)

    def test_quiet_hours_do_not_restrict_silent_actions(self):
        # A silent retry at 03:00 touches nobody; the rule is about commercial
        # communication, not about money movement.
        d = evaluate(req(action_kind=ActionKind.SILENT_RETRY, channel=Channel.NONE,
                         scheduled_for=MIDDAY.replace(hour=3)), POLICY)
        assert not any(r.rule_id == "TRAI_QUIET_HOURS" for r in d.blocks)

    @pytest.mark.parametrize("channel", [Channel.SMS, Channel.WHATSAPP])
    def test_missing_dlt_template_is_blocked(self, channel):
        d = evaluate(req(channel=channel, dlt_template_id=None), POLICY)
        assert any(r.rule_id == "TRAI_DLT_TEMPLATE" for r in d.blocks)

    def test_email_needs_no_dlt_template(self):
        d = evaluate(req(channel=Channel.EMAIL, dlt_template_id=None), POLICY)
        assert not any(r.rule_id == "TRAI_DLT_TEMPLATE" for r in d.blocks)

    def test_absent_consent_is_a_refusal_not_a_default(self):
        d = evaluate(req(consent={}), POLICY)
        assert any(r.rule_id == "DPDP_CONSENT" for r in d.blocks)

    def test_consent_is_per_channel(self):
        d = evaluate(req(channel=Channel.SMS,
                         consent={"whatsapp": True, "sms": False}), POLICY)
        assert any(r.rule_id == "DPDP_CONSENT" for r in d.blocks)


class TestMerchantRules:
    def test_contact_cap_blocks(self):
        d = evaluate(req(), POLICY, MerchantContext(contacts_this_week=3))
        assert any(r.rule_id == "MERCHANT_CONTACT_CAP" for r in d.blocks)

    def test_discount_above_ceiling_blocks(self):
        d = evaluate(req(action_kind=ActionKind.DISCOUNT_OFFER, discount_pct=30.0),
                     POLICY)
        assert any(r.rule_id == "MERCHANT_DISCOUNT_CEILING" for r in d.blocks)

    def test_discount_stacking_is_off_by_default(self):
        # Stacking manufactures the behaviour the system exists to recover from.
        d = evaluate(req(action_kind=ActionKind.DISCOUNT_OFFER, discount_pct=10.0),
                     POLICY, MerchantContext(had_recent_discount=True))
        assert any(r.rule_id == "MERCHANT_DISCOUNT_STACKING" for r in d.blocks)

    def test_disabled_channel_blocks(self):
        restricted = MerchantPolicy("m", allowed_channels=frozenset({Channel.EMAIL}))
        d = evaluate(req(channel=Channel.WHATSAPP), restricted)
        assert any(r.rule_id == "MERCHANT_ALLOWED_CHANNEL" for r in d.blocks)

    def test_blackout_date_blocks(self):
        p = MerchantPolicy("m", blackout_dates=frozenset({date(2026, 6, 10)}))
        d = evaluate(req(), p)
        assert any(r.rule_id == "MERCHANT_BLACKOUT" for r in d.blocks)

    def test_tiny_obligation_blocks(self):
        d = evaluate(req(amount_paise=50), POLICY)
        assert any(r.rule_id == "MERCHANT_MIN_VALUE" for r in d.blocks)


class TestEscalation:
    """Compliant escalation — the brief's wording."""

    def test_above_threshold_escalates_rather_than_allowing(self):
        d = evaluate(req(amount_paise=48_000_00), POLICY)
        assert d.verdict is PolicyVerdict.ESCALATE
        assert any(r.rule_id == "MERCHANT_APPROVAL_THRESHOLD" for r in d.escalations)

    def test_escalation_is_never_a_silent_allow(self):
        d = evaluate(req(amount_paise=48_000_00), POLICY)
        assert not d.allowed

    def test_block_outranks_escalate(self):
        """An illegal, above-threshold action must not reach a human.

        Presenting it for approval would be offering someone the chance to
        authorise something that is still illegal after they say yes.
        """
        d = evaluate(req(amount_paise=48_000_00,
                         scheduled_for=MIDDAY.replace(hour=23)), POLICY)
        assert d.verdict is PolicyVerdict.BLOCK

    def test_below_threshold_proceeds(self):
        assert evaluate(req(amount_paise=1_000_00), POLICY).allowed


class TestRegPackOutranksMerchantPack:
    def test_no_merchant_configuration_can_lift_a_regulatory_block(self):
        """The structural guarantee.

        A merchant may be more restrictive than the regulator, never less — so
        a permissive configuration is tried against every regulatory rule.
        """
        permissive = MerchantPolicy(
            merchant_id="m",
            max_contacts_per_customer_per_week=999,
            max_discount_pct=50.0,
            approval_threshold_paise=10_00_00_000,
            allowed_channels=frozenset(Channel),
            min_obligation_paise=0,
            allow_discount_stacking=True,
        )
        violations = [
            req(scheduled_for=MIDDAY.replace(hour=23)),          # TRAI hours
            req(consent={}),                                      # DPDP
            req(channel=Channel.SMS, dlt_template_id=None),       # DLT
            req(action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE),  # pre-debit
            req(action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE,
                amount_paise=40_000_00,
                predebit_notice_at=MIDDAY - timedelta(hours=25)),  # AFA
            req(action_kind=ActionKind.SILENT_RETRY, channel=Channel.NONE,
                decline_code="MANDATE_REVOKED", attempts_made=0),  # NPCI cap of 0
        ]
        for r in violations:
            d = evaluate(r, permissive)
            assert d.verdict is PolicyVerdict.BLOCK, f"permissive config lifted a block: {r}"

    def test_compiled_settings_are_clamped(self):
        # A configuration bug must degrade toward caution, never widen policy.
        p = compile_from_settings("m", {
            "max_contacts_per_customer_per_week": 10_000,
            "max_discount_pct": 500.0,
            "min_obligation_paise": -5,
        })
        assert p.max_contacts_per_customer_per_week <= 14
        assert p.max_discount_pct <= 50.0
        assert p.min_obligation_paise >= 0

    def test_garbage_settings_fall_back_to_defaults(self):
        p = compile_from_settings("m", {"max_discount_pct": "not a number"})
        assert p.max_discount_pct == 15.0


class TestAdversarial:
    """Actions a competent planner would genuinely propose. All must be stopped.

    Each is locally optimal — the right answer to a narrower question than the
    one the system is actually being asked.
    """

    CASES = [
        ("late-night send at peak responsiveness",
         dict(scheduled_for=MIDDAY.replace(hour=22, minute=15))),
        ("pre-dawn send to beat the inbox",
         dict(scheduled_for=MIDDAY.replace(hour=6))),
        ("SMS with generated copy and no template",
         dict(channel=Channel.SMS, dlt_template_id=None)),
        ("message a customer who never granted consent",
         dict(consent={"email": True})),
        ("voice call to a customer who opted out of voice",
         dict(action_kind=ActionKind.VOICE_CALL, channel=Channel.VOICE,
              consent={"voice": False})),
        ("immediate debit, notice sent minutes ago",
         dict(action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE,
              predebit_notice_at=MIDDAY - timedelta(minutes=10))),
        ("large debit with no additional authentication",
         dict(action_kind=ActionKind.SCHEDULE_DEBIT, channel=Channel.NONE,
              amount_paise=40_000_00,
              predebit_notice_at=MIDDAY - timedelta(hours=30))),
        ("retry a revoked mandate",
         dict(action_kind=ActionKind.SILENT_RETRY, channel=Channel.NONE,
              decline_code="MANDATE_REVOKED")),
        ("one more re-presentation past the NPCI cap",
         dict(action_kind=ActionKind.SILENT_RETRY, channel=Channel.NONE,
              decline_code="AP39", attempts_made=5)),
        ("40% discount to close a stubborn case",
         dict(action_kind=ActionKind.DISCOUNT_OFFER, discount_pct=40.0)),
    ]

    @pytest.mark.parametrize("name,over", CASES, ids=[c[0] for c in CASES])
    def test_adversarial_action_is_blocked(self, name, over):
        d = evaluate(req(**over), POLICY)
        assert d.verdict is PolicyVerdict.BLOCK, f"{name} was not blocked"
        assert d.reasons, "a block must carry at least one reason"

    @pytest.mark.parametrize("name,over", CASES, ids=[c[0] for c in CASES])
    def test_allocator_feasibility_filter_agrees(self, name, over):
        # The allocator must never spend budget on something dispatch will
        # refuse, so the cheap filter and the full gate must agree.
        assert is_feasible(req(**over), POLICY) is False

    def test_zero_escapes_across_the_whole_suite(self):
        escaped = [
            name for name, over in self.CASES
            if evaluate(req(**over), POLICY).verdict is not PolicyVerdict.BLOCK
        ]
        assert escaped == [], f"actions reached dispatch: {escaped}"


class TestExplanations:
    def test_decision_explains_itself(self):
        d = evaluate(req(scheduled_for=MIDDAY.replace(hour=23)), POLICY)
        assert "TRAI_QUIET_HOURS" in d.explain()

    def test_allowed_decision_says_so(self):
        assert evaluate(req(), POLICY).explain() == "allowed"

    def test_every_non_allow_result_produces_a_reason(self):
        d = evaluate(req(scheduled_for=MIDDAY.replace(hour=23), consent={}), POLICY)
        for r in d.results:
            if r.verdict is not PolicyVerdict.ALLOW:
                assert r.reason, f"{r.rule_id} blocked without a reason"

    def test_rule_ids_are_unique_across_packs(self):
        ids = all_rule_ids()
        assert not set(ids["regulatory"]) & set(ids["merchant"])
