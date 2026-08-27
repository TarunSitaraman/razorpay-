"""The oracle's assumptions, and what happens when you change them.

Two jobs. First, prove the parameterisation changed nothing: `DEFAULT_PARAMS`
must reproduce the hard-coded constants exactly, or every previously reported
number quietly moved. Second, prove each swept assumption actually bites — a
sweep axis that does not move the outcome would produce a flat, reassuring
frontier that means nothing at all.

That second failure mode is the dangerous one, because it looks like a robust
result.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from yukti.domain.enums import ActionKind, Channel, UpliftArchetype
from yukti_datagen.response import (
    CONTACT_TOLERANCE,
    DEFAULT_PARAMS,
    IRRITATION,
    MAX_UPLIFT,
    ORGANIC_RECOVERY,
    CaseContext,
    Intervention,
    evaluate,
)

AT = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)
SEED = 20260822


def ctx(
    archetype: UpliftArchetype = UpliftArchetype.PERSUADABLE,
    *,
    case_id: str = "case_test",
    prior_contacts_7d: int = 0,
    decline_code: str = "INSUFFICIENT_FUNDS",
) -> CaseContext:
    return CaseContext(
        case_id=case_id,
        archetype=archetype,
        amount_paise=5_00_000,
        decline_code=decline_code,
        rail_is_mandate=True,
        preferred_channel=Channel.WHATSAPP,
        prior_contacts_7d=prior_contacts_7d,
        open_promise=False,
        in_downtime=False,
    )


MESSAGE = Intervention(ActionKind.MESSAGE, Channel.WHATSAPP, AT)
RETRY = Intervention(ActionKind.SILENT_RETRY, Channel.NONE, AT)


class TestDefaultsAreUnchanged:
    """The refactor must be a no-op on every previously published number."""

    def test_default_params_mirror_the_module_constants(self) -> None:
        assert DEFAULT_PARAMS.organic_recovery == ORGANIC_RECOVERY
        assert DEFAULT_PARAMS.max_uplift == MAX_UPLIFT
        assert DEFAULT_PARAMS.irritation == IRRITATION
        assert DEFAULT_PARAMS.contact_tolerance == CONTACT_TOLERANCE

    @pytest.mark.parametrize("archetype", list(UpliftArchetype))
    def test_explicit_defaults_match_the_implicit_ones(
        self, archetype: UpliftArchetype
    ) -> None:
        """Passing DEFAULT_PARAMS must equal passing nothing, for every archetype."""
        implicit = evaluate(ctx(archetype), MESSAGE, SEED)
        explicit = evaluate(ctx(archetype), MESSAGE, SEED, params=DEFAULT_PARAMS)
        assert implicit == explicit


class TestAssumptionsBite:
    """Each sweep axis must actually move the outcome it claims to move."""

    def test_persuadable_headroom_drives_the_effect(self) -> None:
        """The number the entire thesis rests on."""
        rich = evaluate(ctx(), MESSAGE, SEED, params=DEFAULT_PARAMS)
        poor = evaluate(
            ctx(), MESSAGE, SEED,
            params=DEFAULT_PARAMS.with_max_uplift(UpliftArchetype.PERSUADABLE, 0.03),
        )
        assert rich.uplift > poor.uplift
        # The floor is the assumption, not an artefact: with 0.03 of headroom
        # the causal effect cannot exceed it however well the action is chosen.
        assert poor.uplift <= 0.03 + 1e-9

    def test_sure_thing_headroom_collapses_the_distinction(self) -> None:
        """Give sure things the persuadable's headroom and the two converge.

        This is the axis that matters most to a sceptic: the product exists
        because those two numbers differ. If they do not differ in reality, the
        uplift ranking and the propensity ranking agree and there is nothing to
        sell.
        """
        base = evaluate(ctx(UpliftArchetype.SURE_THING), MESSAGE, SEED)
        lifted = evaluate(
            ctx(UpliftArchetype.SURE_THING), MESSAGE, SEED,
            params=DEFAULT_PARAMS.with_max_uplift(UpliftArchetype.SURE_THING, 0.46),
        )
        assert lifted.uplift > base.uplift

    def test_fatigue_decay_of_one_removes_cross_agent_fatigue(self) -> None:
        """At 1.0 a fourth contact is worth as much as the first."""
        fresh = ctx(prior_contacts_7d=0)
        tired = ctx(prior_contacts_7d=4)

        decayed = evaluate(tired, MESSAGE, SEED)
        assert decayed.uplift < evaluate(fresh, MESSAGE, SEED).uplift

        no_fatigue = DEFAULT_PARAMS.evolve(fatigue_decay=1.0)
        assert evaluate(tired, MESSAGE, SEED, params=no_fatigue).uplift == pytest.approx(
            evaluate(fresh, MESSAGE, SEED, params=no_fatigue).uplift
        )

    def test_sleeping_dog_penalty_can_be_switched_off(self) -> None:
        harmful = evaluate(ctx(UpliftArchetype.SLEEPING_DOG), MESSAGE, SEED)
        assert harmful.uplift < 0

        harmless = evaluate(
            ctx(UpliftArchetype.SLEEPING_DOG), MESSAGE, SEED,
            params=DEFAULT_PARAMS.evolve(sleeping_dog_penalty=0.0),
        )
        assert harmless.uplift >= 0


class TestSilentRetryIsNotFree:
    """The assumption the allocator's costless rule depends on.

    `allocator.lagrangian._take_costless_actions` funds every costless invisible
    action without consulting its margin, arguing that a silent retry has no
    downside branch. In Indian payments that is false — the issuer sends a
    debit-attempt SMS regardless, and a failed mandate presentation can carry a
    bank charge. The policy and its grader currently SHARE that assumption,
    which is why the default evaluation cannot penalise the behaviour.

    Off by default so nothing already published moves; switchable so the sweep
    can price it.
    """

    def test_default_is_off_so_nothing_published_moves(self) -> None:
        assert DEFAULT_PARAMS.silent_retry_irritation == 0.0
        tired = ctx(prior_contacts_7d=4)
        assert evaluate(tired, RETRY, SEED).opted_out is False

    def test_a_retry_can_cause_an_opt_out_once_priced(self) -> None:
        """Over many cases, some fraction must churn — otherwise the axis is inert."""
        priced = DEFAULT_PARAMS.evolve(silent_retry_irritation=0.25)
        opt_outs = sum(
            evaluate(
                ctx(case_id=f"case_{i}", prior_contacts_7d=4), RETRY, SEED, params=priced
            ).opted_out
            for i in range(400)
        )
        assert opt_outs > 0, "silent-retry irritation had no effect on any case"

    def test_a_customer_within_tolerance_is_unaffected(self) -> None:
        """Pricing the risk must not make the first retry punitive."""
        priced = DEFAULT_PARAMS.evolve(silent_retry_irritation=0.25)
        assert all(
            evaluate(
                ctx(case_id=f"case_{i}", prior_contacts_7d=0), RETRY, SEED, params=priced
            ).opted_out
            is False
            for i in range(200)
        )

    def test_contact_opt_outs_are_unchanged_by_the_new_branch(self) -> None:
        """The silent branch must not disturb the contact branch it sits beside."""
        priced = DEFAULT_PARAMS.evolve(silent_retry_irritation=0.25)
        for i in range(100):
            c = ctx(UpliftArchetype.SLEEPING_DOG, case_id=f"case_{i}", prior_contacts_7d=2)
            assert (
                evaluate(c, MESSAGE, SEED, params=priced).opted_out
                == evaluate(c, MESSAGE, SEED).opted_out
            )
