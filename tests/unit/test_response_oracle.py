"""Tests for the counterfactual outcome oracle.

These assert the *causal structure* of the synthetic world. If any of them
break, the evaluation stops meaning anything — a dataset without discoverable
structure would let a propensity model and an uplift model score identically,
which would make the headline result vacuous. So these are correctness tests
for the experiment, not just for the code.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from yukti.domain.enums import ActionKind, Channel, UpliftArchetype
from yukti_datagen.response import (
    MAX_UPLIFT,
    ORGANIC_RECOVERY,
    CaseContext,
    Intervention,
    evaluate,
)

AT = datetime(2026, 5, 3, 19, 0)   # day 3 = post-salary, 19:00 = peak receptivity
SEED = 20260822

SUPPRESS = Intervention(ActionKind.SUPPRESS)
MESSAGE = Intervention(ActionKind.MESSAGE, Channel.WHATSAPP, AT)


def ctx(
    archetype: UpliftArchetype,
    *,
    prior_contacts: int = 0,
    code: str = "INSUFFICIENT_FUNDS",
    downtime: bool = False,
    promise: bool = False,
    amount: int = 250_000,
    case: str = "case_test",
) -> CaseContext:
    return CaseContext(
        case_id=case,
        archetype=archetype,
        amount_paise=amount,
        decline_code=code,
        rail_is_mandate=True,
        preferred_channel=Channel.WHATSAPP,
        prior_contacts_7d=prior_contacts,
        open_promise=promise,
        in_downtime=downtime,
    )


def p(c: CaseContext, iv: Intervention) -> float:
    return evaluate(c, iv, SEED).p_recover


class TestHeadroomIsACeiling:
    """MAX_UPLIFT must bound the achievable effect under every intervention.

    Regression guard: an earlier version multiplied unbounded factors, so a
    perfectly-timed message on a persuadable produced +0.75 uplift against a
    stated headroom of 0.46. That pushed persuadables above sure things on raw
    P(recover) and collapsed the propensity/uplift divergence entirely.
    """

    @pytest.mark.parametrize("archetype", list(UpliftArchetype))
    @pytest.mark.parametrize(
        "iv",
        [
            MESSAGE,
            Intervention(ActionKind.DISCOUNT_OFFER, Channel.WHATSAPP, AT, discount_pct=50),
            Intervention(ActionKind.VOICE_CALL, Channel.VOICE, AT),
            Intervention(ActionKind.SILENT_RETRY, Channel.NONE, AT),
            Intervention(ActionKind.SCHEDULE_DEBIT, Channel.NONE, AT),
        ],
    )
    def test_uplift_never_exceeds_headroom(self, archetype, iv):
        assert evaluate(ctx(archetype), iv, SEED).uplift <= MAX_UPLIFT[archetype] + 1e-9

    @pytest.mark.parametrize("archetype", list(UpliftArchetype))
    def test_probability_stays_in_range(self, archetype):
        for iv in (SUPPRESS, MESSAGE):
            assert 0.0 <= p(ctx(archetype), iv) <= 1.0


class TestPropensityUpliftDivergence:
    """The single property the whole evaluation rests on."""

    def test_propensity_and_uplift_rank_different_archetypes_first(self):
        rows = [
            (a, p(ctx(a), MESSAGE), p(ctx(a), MESSAGE) - p(ctx(a), SUPPRESS))
            for a in UpliftArchetype
        ]
        by_propensity = max(rows, key=lambda r: r[1])[0]
        by_uplift = max(rows, key=lambda r: r[2])[0]
        assert by_propensity is UpliftArchetype.SURE_THING
        assert by_uplift is UpliftArchetype.PERSUADABLE

    def test_sure_thing_is_high_propensity_but_near_zero_uplift(self):
        c = ctx(UpliftArchetype.SURE_THING)
        assert p(c, MESSAGE) > 0.65          # a propensity model loves this customer
        assert p(c, MESSAGE) - p(c, SUPPRESS) < 0.05   # and gains almost nothing

    def test_persuadable_is_low_propensity_but_high_uplift(self):
        c = ctx(UpliftArchetype.PERSUADABLE)
        assert p(c, MESSAGE) - p(c, SUPPRESS) > 0.20


class TestSleepingDogs:
    def test_contact_reduces_recovery(self):
        c = ctx(UpliftArchetype.SLEEPING_DOG)
        assert p(c, MESSAGE) < p(c, SUPPRESS)

    def test_stronger_channel_does_more_damage(self):
        c = ctx(UpliftArchetype.SLEEPING_DOG)
        voice = Intervention(ActionKind.VOICE_CALL, Channel.VOICE, AT)
        email = Intervention(ActionKind.MESSAGE, Channel.EMAIL, AT)
        assert p(c, voice) < p(c, email)

    def test_opt_out_risk_is_far_higher_than_other_archetypes(self):
        # Over many customers, sleeping dogs opt out at a materially higher rate.
        def opt_out_rate(a: UpliftArchetype) -> float:
            n = 400
            outs = sum(
                evaluate(ctx(a, prior_contacts=2, case=f"case_{i}"), MESSAGE, SEED).opted_out
                for i in range(n)
            )
            return outs / n

        assert opt_out_rate(UpliftArchetype.SLEEPING_DOG) > 3 * opt_out_rate(
            UpliftArchetype.SURE_THING
        )


class TestSalaryDayTiming:
    def test_post_salary_retry_beats_mid_month_retry(self):
        c = ctx(UpliftArchetype.PERSUADABLE)

        def at_day(d: int) -> float:
            return p(c, Intervention(ActionKind.SCHEDULE_DEBIT, Channel.NONE, AT.replace(day=d)))

        # This gap is what a fixed +1d/+3d retry cadence cannot exploit.
        assert at_day(3) > at_day(15) * 1.8
        assert at_day(28) > at_day(15)


class TestFatigueIsCrossAgent:
    def test_response_decays_with_prior_contacts(self):
        c0 = ctx(UpliftArchetype.PERSUADABLE, prior_contacts=0)
        c3 = ctx(UpliftArchetype.PERSUADABLE, prior_contacts=3)
        assert p(c3, MESSAGE) < p(c0, MESSAGE)

    def test_decay_is_monotone(self):
        vals = [p(ctx(UpliftArchetype.PERSUADABLE, prior_contacts=n), MESSAGE) for n in range(5)]
        assert vals == sorted(vals, reverse=True)


class TestDowntimeSuppression:
    def test_retry_into_an_outage_mostly_fails(self):
        up = ctx(UpliftArchetype.PERSUADABLE, code="BANK_DOWN", downtime=False)
        down = ctx(UpliftArchetype.PERSUADABLE, code="BANK_DOWN", downtime=True)
        retry = Intervention(ActionKind.SILENT_RETRY, Channel.NONE, AT)
        assert p(down, retry) < p(up, retry) * 0.5

    def test_contacting_during_downtime_is_worse_than_when_healthy(self):
        up = ctx(UpliftArchetype.PERSUADABLE, code="BANK_DOWN", downtime=False)
        down = ctx(UpliftArchetype.PERSUADABLE, code="BANK_DOWN", downtime=True)
        assert p(down, MESSAGE) < p(up, MESSAGE)


class TestPermanentFailures:
    @pytest.mark.parametrize("code", ["MANDATE_REVOKED", "ACCOUNT_CLOSED", "CARD_BLOCKED", "AP08"])
    def test_no_intervention_helps_a_permanent_failure(self, code):
        c = ctx(UpliftArchetype.PERSUADABLE, code=code)
        organic = ORGANIC_RECOVERY[UpliftArchetype.PERSUADABLE]
        retry = Intervention(ActionKind.SILENT_RETRY, Channel.NONE, AT)
        assert p(c, retry) == pytest.approx(organic)


class TestPromiseToPay:
    def test_open_promise_raises_the_do_nothing_baseline(self):
        assert p(ctx(UpliftArchetype.PERSUADABLE, promise=True), SUPPRESS) > p(
            ctx(UpliftArchetype.PERSUADABLE, promise=False), SUPPRESS
        )

    def test_chasing_through_an_open_promise_is_net_negative(self):
        # This is what earns OPEN_PROMISE_TO_PAY its place as a stopping rule
        # rather than a courtesy: the data says chasing loses money.
        c = ctx(UpliftArchetype.PERSUADABLE, promise=True)
        assert p(c, MESSAGE) < p(c, SUPPRESS)


class TestDeterminism:
    def test_same_question_gives_same_answer(self):
        c = ctx(UpliftArchetype.PERSUADABLE)
        a = evaluate(c, MESSAGE, SEED)
        b = evaluate(c, MESSAGE, SEED)
        assert (a.recovered, a.p_recover, a.recovered_paise) == (
            b.recovered, b.p_recover, b.recovered_paise,
        )

    def test_recovery_draw_is_paired_across_interventions(self):
        # The same customer "would have paid at threshold u" regardless of which
        # arm is scoring them, so arms differ only through the probability they
        # induce, never through luck. This pairing is what makes small lift
        # measurable without enormous sample sizes.
        c = ctx(UpliftArchetype.PERSUADABLE)
        strong = evaluate(c, MESSAGE, SEED)
        weak = evaluate(c, SUPPRESS, SEED)
        if weak.recovered:
            assert strong.recovered, "a higher probability must not lose a recovery"

    def test_different_cases_get_different_draws(self):
        outs = {
            evaluate(ctx(UpliftArchetype.PERSUADABLE, case=f"c{i}"), MESSAGE, SEED).recovered
            for i in range(50)
        }
        assert outs == {True, False}


class TestDiscountShape:
    def test_larger_discounts_help_but_with_diminishing_returns(self):
        c = ctx(UpliftArchetype.PERSUADABLE)

        def at_pct(x: float) -> float:
            return p(c, Intervention(ActionKind.DISCOUNT_OFFER, Channel.WHATSAPP, AT, discount_pct=x))

        first_10 = at_pct(10) - at_pct(0)
        next_10 = at_pct(20) - at_pct(10)
        assert first_10 > next_10 > 0

    def test_discount_is_netted_off_recovered_amount(self):
        c = ctx(UpliftArchetype.SURE_THING, amount=100_000)
        out = evaluate(
            c, Intervention(ActionKind.DISCOUNT_OFFER, Channel.WHATSAPP, AT, discount_pct=10), SEED
        )
        if out.recovered:
            assert out.recovered_paise == 90_000
