"""The estimator, on data whose answer is known by construction.

These are the tests that decide whether the headline number can be trusted. The
harness can run beautifully and produce a figure that is quietly wrong, and the
only defence is checking the arithmetic against cases where the right answer is
arithmetic rather than judgement.
"""

from __future__ import annotations

import random

import pytest

from yukti.eval.estimator import (
    Interval,
    bootstrap_per_1k,
    holdout_estimate,
    summarise,
)
from yukti.eval.oracle_bridge import ArmOutcome


def outcome(case_id: str, customer_id: str, *, margin: int, recovered: bool = False,
            recovered_paise: int = 0, discount: int = 0, channel_cost: int = 0,
            action: str = "message", channel: str = "sms",
            opted_out: bool = False) -> ArmOutcome:
    return ArmOutcome(
        case_id=case_id, customer_id=customer_id, action_kind=action,
        channel=channel, recovered=recovered, opted_out=opted_out,
        recovered_paise=recovered_paise, discount_paise=discount,
        channel_cost_paise=channel_cost, net_margin_paise=margin, true_uplift=0.0,
    )


class TestBootstrap:
    def test_a_constant_effect_has_a_zero_width_interval(self):
        by_customer = {f"c{i}": [100] for i in range(200)}
        interval = bootstrap_per_1k(by_customer)
        assert interval.point == pytest.approx(100_000)
        assert interval.low == interval.high == pytest.approx(100_000)

    def test_a_noisy_effect_has_a_real_interval_containing_the_point(self):
        rng = random.Random(7)
        by_customer = {f"c{i}": [int(rng.gauss(50, 400))] for i in range(500)}
        interval = bootstrap_per_1k(by_customer)
        assert interval.low < interval.point < interval.high

    def test_a_null_effect_is_called_significant_about_5pc_of_the_time(self):
        """Coverage, measured over many seeds — not asserted on one.

        A 95% interval is SUPPOSED to exclude zero on about one null dataset in
        twenty. Asserting that a single seed includes zero tests nothing: the
        first seed tried here produced a -1.9 sigma sample and a CI that
        excluded zero, which is the estimator behaving exactly as specified.

        The same mistake as the day-3 gate, where one split would have reported
        a failure that seven splits showed was noise. The property worth
        pinning is the RATE.
        """
        false_positives = 0
        trials = 120
        for seed in range(trials):
            rng = random.Random(seed)
            by_customer = {f"c{i}": [int(rng.gauss(0, 300))] for i in range(600)}
            if bootstrap_per_1k(by_customer, rounds=400, seed=seed).excludes_zero:
                false_positives += 1

        rate = false_positives / trials
        # Generous bounds: 120 trials cannot pin 5% tightly, and the point is to
        # catch an estimator that is badly miscalibrated in either direction —
        # one that never rejects is as broken as one that always does.
        assert 0.0 < rate < 0.20, (
            f"null effect called significant {rate:.1%} of the time; a 95% "
            f"interval should be near 5%"
        )

    def test_a_real_effect_is_detected(self):
        """The other half of calibration: an interval that never excludes zero
        would be useless however honest."""
        detected = 0
        trials = 40
        for seed in range(trials):
            rng = random.Random(seed)
            by_customer = {f"c{i}": [int(rng.gauss(400, 300))] for i in range(600)}
            if bootstrap_per_1k(by_customer, rounds=400, seed=seed).excludes_zero:
                detected += 1
        assert detected == trials, f"only {detected}/{trials} clear effects detected"

    def test_customers_are_the_resampling_unit_not_cases(self):
        """One customer with many correlated cases must not narrow the interval.

        This is the difference between an honest interval and a flattering one:
        treating a single person's three obligations as three independent
        observations understates the variance, and a lift number whose whole
        purpose is to be trusted must not err in that direction.
        """
        # Ten customers, each holding twenty perfectly correlated cases. The
        # effective sample size is ten, not two hundred.
        clustered = {f"c{i}": [1000 if i % 2 else -1000] * 20 for i in range(10)}
        clustered_iv = bootstrap_per_1k(clustered)

        # The same two hundred observations spread over two hundred customers.
        spread = {f"c{i}": [1000 if i % 2 else -1000] for i in range(200)}
        spread_iv = bootstrap_per_1k(spread)

        clustered_width = clustered_iv.high - clustered_iv.low
        spread_width = spread_iv.high - spread_iv.low
        assert clustered_width > spread_width * 2, (
            "clustering was ignored — the interval is narrower than the data "
            "supports"
        )

    def test_an_empty_population_does_not_explode(self):
        assert bootstrap_per_1k({}) == Interval(0.0, 0.0, 0.0)

    def test_the_interval_is_deterministic_for_a_given_seed(self):
        by_customer = {f"c{i}": [i * 7 % 500] for i in range(300)}
        assert bootstrap_per_1k(by_customer, seed=1) == bootstrap_per_1k(
            by_customer, seed=1)


class TestSummarise:
    def _baseline(self, ids, margin=0):
        return {i: outcome(i, f"cust_{i}", margin=margin, action="suppress",
                           channel="none") for i in ids}

    def test_incremental_is_measured_against_the_same_case_untouched(self):
        """Not against the population average — against that customer's own
        counterfactual, which is what the paired design buys."""
        ids = ["a", "b"]
        baseline = self._baseline(ids, margin=100)
        outcomes = [outcome("a", "cust_a", margin=500),
                    outcome("b", "cust_b", margin=50)]

        m = summarise("X", outcomes, baseline, {})

        # (500-100) + (50-100) = 350
        assert m.true_incremental_margin_paise == 350

    def test_recoveries_that_would_have_happened_anyway_are_counted(self):
        """The number that earns the merchant's trust."""
        baseline = {
            "a": outcome("a", "c1", margin=0, recovered=True, action="suppress"),
            "b": outcome("b", "c2", margin=0, recovered=False, action="suppress"),
        }
        outcomes = [
            outcome("a", "c1", margin=10, recovered=True),
            outcome("b", "c2", margin=10, recovered=True),
        ]

        m = summarise("X", outcomes, baseline, {})

        assert m.recovered_cases == 2
        assert m.would_have_recovered_anyway == 1, (
            "a recovery that would have happened without us was billed as caused"
        )

    def test_only_opt_outs_we_caused_are_counted(self):
        """An opt-out that would have happened anyway is not a cost of this policy."""
        baseline = {
            "a": outcome("a", "c1", margin=0, opted_out=True, action="suppress"),
            "b": outcome("b", "c2", margin=0, opted_out=False, action="suppress"),
        }
        outcomes = [outcome("a", "c1", margin=0, opted_out=True),
                    outcome("b", "c2", margin=0, opted_out=True)]

        assert summarise("X", outcomes, baseline, {}).opt_outs == 1

    def test_spend_is_charged_whether_or_not_the_recovery_lands(self):
        """The allocator commits money before knowing the result, so the
        evaluation must charge it the same way — otherwise Yukti is scored
        against a cost model it never optimised against."""
        baseline = self._baseline(["a"])
        outcomes = [outcome("a", "c1", margin=-75, recovered=False,
                            channel_cost=75)]

        m = summarise("X", outcomes, baseline, {})

        assert m.channel_spend_paise == 75
        assert m.true_incremental_margin_paise == -75

    def test_suppression_counts_as_no_action(self):
        baseline = self._baseline(["a", "b"])
        outcomes = [outcome("a", "c1", margin=0, action="suppress", channel="none"),
                    outcome("b", "c2", margin=10)]

        m = summarise("X", outcomes, baseline, {})

        assert m.actions_taken == 1
        assert m.contacts == 1


class TestHoldoutEstimator:
    """Can a 10% holdout recover a known truth?

    The whole incrementality claim rests on this being checkable rather than
    asserted, so it is checked against data whose answer is arithmetic.
    """

    def _population(self, n: int, treated_margin: int, held_margin: int):
        outcomes, baseline, assigned = [], {}, {}
        for i in range(n):
            cid, cust = f"case_{i}", f"cust_{i}"
            is_held = i % 10 == 0            # a clean 10%
            assigned[cid] = "holdout" if is_held else "treatment"
            margin = held_margin if is_held else treated_margin
            outcomes.append(outcome(cid, cust, margin=margin))
            baseline[cid] = outcome(cid, cust, margin=held_margin, action="suppress")
        return outcomes, baseline, assigned

    def test_it_recovers_a_known_uniform_effect(self):
        outcomes, baseline, assigned = self._population(1000, 150, 100)

        result = holdout_estimate(outcomes, baseline, assigned, total_cases=1000)

        # Treated mean 150, held mean 100 -> 50 per case over 1,000 cases.
        assert result.point == pytest.approx(50 * 1000, rel=0.01)

    def test_the_interval_covers_the_truth(self):
        outcomes, baseline, assigned = self._population(1000, 150, 100)
        result = holdout_estimate(outcomes, baseline, assigned, total_cases=1000)
        assert result.low <= 50_000 <= result.high

    def test_a_null_effect_is_not_reported_as_a_win(self):
        outcomes, baseline, assigned = self._population(1000, 100, 100)
        result = holdout_estimate(outcomes, baseline, assigned, total_cases=1000)
        assert not result.excludes_zero

    def test_no_holdout_means_no_estimate_rather_than_a_wrong_one(self):
        outcomes, baseline, assigned = self._population(100, 150, 100)
        assigned = {k: "treatment" for k in assigned}

        result = holdout_estimate(outcomes, baseline, assigned, total_cases=100)

        assert result == Interval(0.0, 0.0, 0.0), (
            "an estimate was produced with nothing to compare against"
        )

    def test_the_estimate_is_deterministic(self):
        outcomes, baseline, assigned = self._population(500, 150, 100)
        a = holdout_estimate(outcomes, baseline, assigned, 500, seed=5)
        b = holdout_estimate(outcomes, baseline, assigned, 500, seed=5)
        assert a == b
