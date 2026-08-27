"""The sensitivity sweep, and the properties that make it worth believing.

A sweep is only evidence if it is capable of producing a bad answer. These tests
check the machinery that gives it that capability: that the leak guard still
applies on the in-memory path, that each axis moves the world it claims to move,
that the population composition is what was asked for, and that `crossover`
actually reports a sign change rather than always returning something reassuring.

The sweep itself is slow (it refits a gradient-boosted model at every grid
point), so the end-to-end run is marked and kept small.
"""

from __future__ import annotations

import pytest
from yukti.domain.enums import UpliftArchetype
from yukti.eval import sensitivity as sens
from yukti.eval.sensitivity import SweepPoint, World, crossover, world_at
from yukti.intelligence.features import FORBIDDEN
from yukti_datagen.response import DEFAULT_PARAMS


class TestPopulation:
    def test_archetype_mix_is_honoured(self) -> None:
        """The composition is a sweep axis, so it has to be the requested one."""
        mix = {
            UpliftArchetype.SURE_THING: 0.5,
            UpliftArchetype.PERSUADABLE: 0.5,
            UpliftArchetype.LOST_CAUSE: 0.0,
            UpliftArchetype.SLEEPING_DOG: 0.0,
        }
        cases = sens.build_population(World(mix=mix), 600, seed=4)
        seen = {c.ctx.archetype for c in cases}
        assert UpliftArchetype.LOST_CAUSE not in seen
        assert UpliftArchetype.SLEEPING_DOG not in seen

    def test_some_customers_hold_several_obligations(self) -> None:
        """Otherwise the per-customer contact cap never binds and the
        cross-surface arbitration the product is about is never exercised."""
        cases = sens.build_population(World(), 800, seed=11)
        per_customer: dict[str, int] = {}
        for c in cases:
            per_customer[c.customer_id] = per_customer.get(c.customer_id, 0) + 1
        assert max(per_customer.values()) > 1

    def test_population_is_deterministic_for_a_seed(self) -> None:
        a = sens.build_population(World(), 200, seed=3)
        b = sens.build_population(World(), 200, seed=3)
        assert [c.ctx for c in a] == [c.ctx for c in b]


class TestFeatureLeakage:
    """The in-memory path must be guarded exactly as the database path is.

    It shares `frame_from_rows` precisely so this cannot drift — but the
    observable rows are assembled here, and they carry `archetype`, so the guard
    is doing real work rather than being decorative.
    """

    def test_no_forbidden_column_reaches_the_sweep_frame(self) -> None:
        cases = sens.build_population(World(), 300, seed=5)
        frame = sens.exploration_frame(World(), cases, seed=5)
        assert not (FORBIDDEN & set(frame.X.columns))

    def test_archetype_is_carried_for_scoring_but_not_for_learning(self) -> None:
        cases = sens.build_population(World(), 300, seed=5)
        frame = sens.exploration_frame(World(), cases, seed=5)
        assert "archetype" not in frame.X.columns
        assert len(frame.archetype) == len(frame.X)

    def test_both_arms_are_present(self) -> None:
        """An exploration frame with no control arm cannot identify an effect."""
        cases = sens.build_population(World(), 500, seed=6)
        frame = sens.exploration_frame(World(), cases, seed=6)
        assert frame.treated.sum() > 0
        assert (1 - frame.treated).sum() > 0


class TestAxes:
    @pytest.mark.parametrize("axis", list(sens.AXES))
    def test_every_axis_is_constructible(self, axis: str) -> None:
        assert isinstance(world_at(axis, 0.1), World)

    def test_unknown_axis_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            world_at("not_an_axis", 0.1)

    def test_persuadable_axis_moves_only_the_persuadable(self) -> None:
        w = world_at("persuadable_uplift", 0.12)
        assert w.params.max_uplift[UpliftArchetype.PERSUADABLE] == 0.12
        assert (
            w.params.max_uplift[UpliftArchetype.SURE_THING]
            == DEFAULT_PARAMS.max_uplift[UpliftArchetype.SURE_THING]
        )

    def test_sleeping_dog_axis_holds_the_prize_fixed(self) -> None:
        """The share is taken from sure things, not from persuadables.

        If it came out of the persuadable pool the axis would be measuring the
        size of the opportunity and the cost of the harm at the same time, and
        the frontier would be uninterpretable.
        """
        w = world_at("sleeping_dog_share", 0.04)
        assert w.mix[UpliftArchetype.SLEEPING_DOG] == 0.04
        assert w.mix[UpliftArchetype.PERSUADABLE] == pytest.approx(0.25)
        assert sum(w.mix.values()) == pytest.approx(1.0)


class TestCrossover:
    """Reporting where the thesis fails is the point of the whole module."""

    @staticmethod
    def _point(value: float, y: int, rival: int) -> SweepPoint:
        def result(arm: str, inc: int) -> sens.ArmResult:
            from yukti.eval.estimator import Interval

            # Both margins carry the same value: these tests exercise the
            # crossover arithmetic, and `margin_over_rival` reads the
            # contact-attributable one.
            return sens.ArmResult(
                arm=arm, contacts=0, recovered_cases=0, opt_outs=0,
                incremental_paise=inc, contact_attributable_paise=inc,
                per_1k=Interval(float(inc), 0.0, 0.0),
            )

        return SweepPoint(
            axis="test", value=value,
            arms={"Y": result("Y", y), "B3": result("B3", rival),
                  "B1": result("B1", 0), "B4": result("B4", 0)},
        )

    def test_finds_the_sign_change(self) -> None:
        points = [self._point(0.4, 100, 50), self._point(0.2, 40, 60)]
        x = crossover(points)
        assert x is not None
        assert 0.2 < x < 0.4

    def test_returns_none_when_the_thesis_never_fails(self) -> None:
        points = [self._point(0.4, 100, 50), self._point(0.2, 90, 40)]
        assert crossover(points) is None

    def test_margin_over_rival_is_negative_when_beaten(self) -> None:
        assert self._point(0.1, 40, 60).margin_over_rival() == -20
        assert self._point(0.1, 40, 60).winner == "B3"


@pytest.mark.slow
class TestEndToEnd:
    """One tiny sweep, to prove the pieces compose.

    Deliberately not asserting who wins: this test exists to catch a broken
    pipeline, and a test that required Niyama to win would be the exact
    circularity the module was written to expose.
    """

    def test_a_two_point_sweep_produces_comparable_arms(self) -> None:
        points = sens.sweep(
            "persuadable_uplift", (0.46, 0.06),
            n_train=1_200, n_plan=600, contact_budget=25,
        )
        assert len(points) == 2
        for p in points:
            assert set(p.arms) == set(sens.ARM_KEYS)
            assert p.winner in sens.ARM_KEYS

    def test_shrinking_the_prize_shrinks_the_advantage(self) -> None:
        """The one directional claim that must hold, or the sweep is inert.

        With less headroom on persuadables there is less causal effect for any
        arm to capture, so the money Niyama makes over doing nothing has to fall.
        """
        points = sens.sweep(
            "persuadable_uplift", (0.46, 0.06),
            n_train=1_200, n_plan=600, contact_budget=25,
        )
        rich, poor = points
        assert rich.arms["Y"].incremental_paise > poor.arms["Y"].incremental_paise


class TestCrossoverDirection:
    """Which side of the crossover the thesis fails on.

    Got this wrong twice while building the sweep, both times in the direction
    that reads as a stronger result. Most axes fail as the value FALLS;
    `sure_thing_uplift` fails as it RISES, because once customers who would have
    paid anyway also respond to contact, propensity and uplift rank alike.
    """

    def test_descending_grid_that_wins_early_fails_below(self) -> None:
        points = [
            TestCrossover._point(0.46, 100, 10),
            TestCrossover._point(0.03, 10, 60),
        ]
        assert sens.crossover_direction(points) == "below"

    def test_ascending_grid_that_wins_early_fails_above(self) -> None:
        points = [
            TestCrossover._point(0.04, 100, 10),
            TestCrossover._point(0.34, 10, 60),
        ]
        assert sens.crossover_direction(points) == "above"

    def test_ascending_grid_that_loses_early_fails_below(self) -> None:
        points = [
            TestCrossover._point(0.04, 10, 60),
            TestCrossover._point(0.34, 100, 10),
        ]
        assert sens.crossover_direction(points) == "below"

    def test_single_point_grid_does_not_crash(self) -> None:
        assert sens.crossover_direction([TestCrossover._point(0.1, 1, 0)]) == "below"
