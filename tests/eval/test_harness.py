"""The harness properties that make the comparison mean anything.

Each of these is a way the evaluation could produce a beautiful, wrong number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from yukti.domain.enums import ActionKind, Channel, UpliftArchetype
from yukti.domain.ids import case_id, customer_id, obligation_id
from yukti.eval import oracle_bridge
from yukti.eval.arms import ARMS, BY_KEY, HOLDOUT
from yukti_datagen.response import CaseContext, evaluate, Intervention

AS_OF = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
SEED = 20260822


def context(archetype: str = "persuadable", **kw) -> CaseContext:
    defaults = dict(
        case_id="case_1", archetype=UpliftArchetype(archetype), amount_paise=250_000,
        decline_code="INSUFFICIENT_FUNDS", rail_is_mandate=True,
        preferred_channel=Channel.WHATSAPP, prior_contacts_7d=0,
        open_promise=False, in_downtime=False,
    )
    defaults.update(kw)
    return CaseContext(**defaults)


def facts(ctx: CaseContext, mdr_bps: int = 200) -> oracle_bridge.CaseFacts:
    return oracle_bridge.CaseFacts(
        case_id=ctx.case_id, customer_id="cust_1", merchant_id="mrc_1",
        assigned_arm="treatment", amount_paise=ctx.amount_paise,
        obligation_kind="subscription_cycle", mdr_bps=mdr_bps,
        archetype=ctx.archetype.value, context=ctx,
    )


class TestArms:
    def test_exactly_one_arm_does_not_act(self):
        """The holdout is the denominator; two of them would be a bug."""
        assert sum(1 for a in ARMS if not a.acts) == 1
        assert not HOLDOUT.acts

    def test_every_acting_arm_has_a_distinct_scorer_type(self):
        """Two arms sharing a scorer would silently be the same experiment."""
        types = [type(a.scorer()) for a in ARMS if a.acts]
        assert len(set(types)) == len(types)

    def test_the_rival_is_propensity(self):
        """B3 is the arm whose defeat is the actual claim. If it stopped being
        propensity, the headline would be comparing against nothing in particular."""
        from yukti.scoring import PropensityScorer
        assert isinstance(BY_KEY["B3"].scorer(), PropensityScorer)


class TestPairedComparison:
    """The property the whole design rests on.

    The oracle keys its recovery draw on the CASE, not the intervention. So the
    same customer "would have paid at threshold u" whichever arm is scoring
    them, and arms differ only through the probability they induce — never
    through luck. Without this, measuring a few points of lift would need
    enormous samples.
    """

    def test_the_same_case_gets_the_same_draw_across_interventions(self):
        ctx = context()
        a = evaluate(ctx, Intervention(kind=ActionKind.SUPPRESS), SEED)
        b = evaluate(ctx, Intervention(kind=ActionKind.MESSAGE,
                                       channel=Channel.WHATSAPP), SEED)
        # Different probabilities...
        assert a.p_recover != b.p_recover
        # ...but the outcomes are consistent with one shared threshold: a higher
        # probability can never recover LESS often on the same case.
        if a.recovered:
            assert b.recovered, "a better intervention un-recovered the same case"

    def test_different_cases_get_different_draws(self):
        """Otherwise every case in a run would resolve identically."""
        outcomes = {
            evaluate(context(case_id=f"case_{i}"),
                     Intervention(kind=ActionKind.MESSAGE, channel=Channel.SMS),
                     SEED).recovered
            for i in range(30)
        }
        assert len(outcomes) == 2, "all cases resolved the same way"

    def test_scoring_is_reproducible(self):
        """`make eval` twice must give identical numbers."""
        f = facts(context())
        first = oracle_bridge.score(f, "message", "whatsapp", AS_OF, 0.0, SEED)
        second = oracle_bridge.score(f, "message", "whatsapp", AS_OF, 0.0, SEED)
        assert first == second


class TestMarginArithmetic:
    def test_mdr_is_deducted_from_recovered_revenue(self):
        """A recovered rupee is not a kept rupee."""
        f = facts(context(), mdr_bps=200)
        assert f.margin_of(100_000, 0, 0) == 98_000

    def test_discount_and_channel_cost_are_charged_in_full(self):
        f = facts(context(), mdr_bps=0)
        assert f.margin_of(100_000, 5_000, 75) == 94_925

    def test_a_failed_contact_still_costs_its_channel(self):
        """The allocator commits the spend before knowing the outcome, so the
        evaluation must charge it the same way."""
        f = facts(context(), mdr_bps=0)
        assert f.margin_of(0, 0, 900) == -900

    def test_discount_is_charged_on_the_offer_not_the_conversion(self):
        """Matches how the allocator prices it — and matches the day-5 fix that
        separated the offered discount from the realised payout."""
        f = facts(context())
        out = oracle_bridge.score(f, "discount_offer", "whatsapp", AS_OF, 10.0, SEED)
        assert out.discount_paise == 25_000     # 10% of 250,000, regardless


class TestHoldoutScoring:
    def test_the_holdout_takes_no_action(self):
        f = facts(context())
        out = oracle_bridge.score_no_action(f, SEED)
        assert out.action_kind == ActionKind.SUPPRESS.value
        assert out.channel == Channel.NONE.value
        assert out.discount_paise == 0
        assert out.channel_cost_paise == 0

    def test_a_sure_thing_recovers_in_the_holdout(self):
        """If nobody recovered without treatment, every arm would look like a
        hero and the denominator would be meaningless."""
        recovered = sum(
            oracle_bridge.score_no_action(
                facts(context("sure_thing", case_id=f"c{i}")), SEED).recovered
            for i in range(40)
        )
        assert recovered > 20, "sure things are not recovering organically"

    def test_a_lost_cause_does_not_recover_even_when_treated(self):
        treated = sum(
            oracle_bridge.score(
                facts(context("lost_cause", case_id=f"c{i}")),
                "discount_offer", "whatsapp", AS_OF, 25.0, SEED).recovered
            for i in range(40)
        )
        assert treated <= 4, "lost causes are recovering — the archetype is not lost"

    def test_contacting_a_sleeping_dog_destroys_value(self):
        """The archetype that makes over-contacting visibly cost money."""
        uplifts = [
            oracle_bridge.score(
                facts(context("sleeping_dog", case_id=f"c{i}", prior_contacts_7d=3)),
                "message", "whatsapp", AS_OF, 0.0, SEED).true_uplift
            for i in range(20)
        ]
        assert sum(uplifts) / len(uplifts) < 0, "contacting a sleeping dog helped"
