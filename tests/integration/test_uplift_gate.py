"""The gate: uplift must beat propensity, and for the right reason.

These assert the project's central claim on held-out data. If they fail, the
allocator is being built on a model that cannot rank and the honest response is
to stop, not to tune until the number looks better.
"""

from __future__ import annotations

import numpy as np
import pytest
from yukti.intelligence.features import Frame, build_frame
from yukti.intelligence.uplift import (
    PropensityBaseline,
    XLearner,
    auuc,
    qini_curve,
    uplift_at_k,
)

pytestmark = pytest.mark.integration

SEED = 20260822


def _split(frame: Frame, seed: int = SEED, test_frac: float = 0.3):
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(frame))
    cut = int(len(frame) * (1 - test_frac))

    def take(sel):
        return Frame(
            X=frame.X.iloc[sel].reset_index(drop=True),
            treated=frame.treated.iloc[sel].reset_index(drop=True),
            outcome=frame.outcome.iloc[sel].reset_index(drop=True),
            archetype=frame.archetype.iloc[sel].reset_index(drop=True),
            case_id=frame.case_id.iloc[sel].reset_index(drop=True),
            amount_paise=frame.amount_paise.iloc[sel].reset_index(drop=True),
            cost_paise=frame.cost_paise.iloc[sel].reset_index(drop=True),
        )

    return take(np.sort(idx[:cut])), take(np.sort(idx[cut:]))


@pytest.fixture(scope="module")
def fitted():
    from yukti.store.db import connect

    with connect() as conn:
        try:
            frame = build_frame(conn)
        except SystemExit:
            pytest.skip("no labelled cases — run `make seed && make history`")

    if len(frame) < 3000:
        pytest.skip("dataset too small for a meaningful gate")

    train, test = _split(frame)
    x_model = XLearner(SEED).fit(train)
    propensity = PropensityBaseline(SEED).fit(train)
    return {
        "test": test,
        "uplift": x_model.predict(test),
        "propensity": propensity.predict(test),
        "t": test.treated.to_numpy(),
        "y": test.outcome.to_numpy(),
        "arch": test.archetype.to_numpy(),
    }


class TestTheGate:
    def test_uplift_beats_propensity_on_auuc(self, fitted):
        u = auuc(fitted["uplift"].uplift, fitted["t"], fitted["y"])
        p = auuc(fitted["propensity"], fitted["t"], fitted["y"])
        assert u > p, f"uplift {u:.3f} did not beat propensity {p:.3f}"

    def test_uplift_beats_random_targeting(self, fitted):
        rand = np.random.default_rng(SEED).random(len(fitted["t"]))
        u = auuc(fitted["uplift"].uplift, fitted["t"], fitted["y"])
        assert u > auuc(rand, fitted["t"], fitted["y"])

    def test_uplift_ranks_persuadables_above_sure_things(self, fitted):
        """Archetype separation — the half that a situational-only model fails.

        A model can learn "suppress during downtime, skip permanent declines,
        respect fatigue" and clear an AUUC bar without ever distinguishing a
        customer who needs a nudge from one who was going to pay anyway. That
        distinction is the product, so it is asserted separately.
        """
        arch, scores = fitted["arch"], fitted["uplift"].uplift
        persuadable = scores[arch == "persuadable"].mean()
        sure_thing = scores[arch == "sure_thing"].mean()
        assert persuadable > sure_thing

    def test_sleeping_dogs_score_negative(self, fitted):
        # Contacting them destroys value, so the model must say so rather than
        # merely rank them low.
        scores = fitted["uplift"].uplift
        assert scores[fitted["arch"] == "sleeping_dog"].mean() < 0

    def test_propensity_inverts_the_ranking(self, fitted):
        # The failure mode being guarded against: a conventional model spends
        # the budget on customers who were going to pay anyway.
        arch, prop = fitted["arch"], fitted["propensity"]
        assert prop[arch == "sure_thing"].mean() > prop[arch == "persuadable"].mean()

    def test_incremental_recovery_is_positive_at_the_top_of_the_ranking(self, fitted):
        assert uplift_at_k(fitted["uplift"].uplift, fitted["t"], fitted["y"], 0.30) > 0.02


class TestAuucImplementation:
    """The metric itself, validated against known ground truth.

    Written after a bug where the Qini rescaling was unbounded at shallow depth
    and random targeting outscored every real model.
    """

    @pytest.fixture(scope="class")
    def synthetic(self):
        rng = np.random.default_rng(0)
        n = 20_000
        true_uplift = np.where(rng.random(n) < 0.5, 0.30, 0.0)
        base = rng.uniform(0.1, 0.5, n)
        treated = (rng.random(n) < 0.75).astype(int)   # same imbalance as real data
        y = (rng.random(n) < np.clip(base + treated * true_uplift, 0, 1)).astype(int)
        return true_uplift, base, treated, y

    def test_oracle_scores_highest(self, synthetic):
        tu, base, t, y = synthetic
        rng = np.random.default_rng(1)
        assert auuc(tu + rng.normal(0, 1e-6, len(tu)), t, y) > auuc(rng.random(len(tu)), t, y)

    def test_anti_oracle_scores_lowest(self, synthetic):
        tu, base, t, y = synthetic
        rng = np.random.default_rng(2)
        assert auuc(-tu + rng.normal(0, 1e-6, len(tu)), t, y) < auuc(rng.random(len(tu)), t, y)

    def test_random_is_near_zero_relative_to_the_oracle(self, synthetic):
        tu, base, t, y = synthetic
        rng = np.random.default_rng(3)
        oracle = auuc(tu + rng.normal(0, 1e-6, len(tu)), t, y)
        rand = auuc(rng.random(len(tu)), t, y)
        assert abs(rand) < 0.15 * abs(oracle)

    def test_qini_curve_is_stable_at_shallow_depth(self, synthetic):
        # The bug: n_t/n_c explodes when the top slice holds almost no control
        # rows, so the curve swung wildly and random targeting won.
        tu, base, t, y = synthetic
        _, gains = qini_curve(np.random.default_rng(4).random(len(t)), t, y)
        head = gains[: max(1, len(gains) // 100)]
        assert np.all(np.abs(head) < np.abs(gains).max())


class TestCalibration:
    def test_treated_arm_is_calibrated(self, fitted):
        """The allocator consumes expected VALUE, not a ranking.

        A miscalibrated probability turns straight into a wrong budget decision,
        and no ranking metric would ever surface it.
        """
        p = fitted["uplift"].p_treated[fitted["t"] == 1]
        obs = fitted["y"][fitted["t"] == 1]
        edges = np.quantile(p, np.linspace(0, 1, 6))
        for lo, hi in zip(edges[:-1], edges[1:], strict=False):
            m = (p >= lo) & (p <= hi)
            if m.sum() < 30:
                continue
            assert abs(p[m].mean() - obs[m].mean()) < 0.15
