"""No feature may predict the outcome by knowing the outcome.

`test_feature_leakage.py` guards the archetype — the latent label. This guards a
different and sneakier class: a column that describes the ACTION but was
recorded conditionally on the result.

The instance that motivated it: `recovery_action.discount_paise` was written
only when the case recovered, because a discount costs nothing if the customer
never pays. Economically correct, and as a feature it meant `discount_paise > 0`
predicted recovery with 100% accuracy across 1,936 rows.

It survived two days of testing because `TLearner` and `XLearner` strip the
action columns from both arms, so no model in the gate could see it. It surfaced
only when `ActionConditionalUplift` — which keeps those columns deliberately —
scored a 5% discount at +0.92 uplift while every other action scored negative.

So the check is not "is this one column safe" but "does any single feature
separate the outcome far better than a real causal effect could".
"""

from __future__ import annotations

import numpy as np
import pytest

from yukti.intelligence.features import build_frame

# Above this, a single column is not measuring an effect — it is reading the
# answer. Real uplift in this dataset tops out well under 0.5, and the strongest
# honest single predictor (decline transience) lands far below.
LEAK_THRESHOLD = 0.92

# Columns whose whole job is to correlate with the outcome, and which are held
# out of the feature matrix anyway.
EXEMPT = frozenset({"treated"})


@pytest.fixture(scope="module")
def frame():
    from yukti.store.db import connect
    with connect() as conn:
        return build_frame(conn, limit=40_000)


def _binary_separation(values: np.ndarray, outcome: np.ndarray) -> float:
    """P(recovered | feature is non-zero), for a column split at zero.

    Deliberately crude. A leak of this kind is not subtle — it is a column that
    is present exactly when the label is 1 — and a crude statistic that anyone
    can reproduce in SQL is worth more here than a calibrated one.
    """
    mask = values > 0
    if mask.sum() < 50 or (~mask).sum() < 50:
        return 0.0
    return float(outcome[mask].mean())


def test_no_numeric_feature_reads_the_outcome(frame):
    offenders = []
    outcome = frame.outcome.to_numpy()

    for col in frame.X.columns:
        if col in EXEMPT:
            continue
        series = frame.X[col]
        if str(series.dtype) == "category":
            continue
        values = series.to_numpy(dtype=float, na_value=0.0)
        separation = _binary_separation(values, outcome)
        if separation >= LEAK_THRESHOLD:
            offenders.append((col, round(separation, 4)))

    assert not offenders, (
        f"feature(s) predict the outcome almost perfectly: {offenders}. "
        "A column recorded conditionally on the result is a target leak, not a "
        "strong signal — check whether it is written only when the case recovered."
    )


def test_the_specific_regression_discount_is_the_offer_not_the_payout(frame):
    """Pinned by name, because this is the one that actually happened."""
    if "discount_paise" not in frame.X.columns:
        pytest.skip("discount_paise is not in the frame")

    values = frame.X["discount_paise"].to_numpy(dtype=float, na_value=0.0)
    outcome = frame.outcome.to_numpy()
    offered = values > 0

    assert offered.sum() > 100, "not enough discount rows to judge"
    recovery_rate = float(outcome[offered].mean())
    assert recovery_rate < LEAK_THRESHOLD, (
        f"{recovery_rate:.1%} of rows with a recorded discount recovered. "
        "discount_paise is recording what was PAID, not what was OFFERED."
    )


def test_offered_discounts_exist_for_unrecovered_cases(frame):
    """The direct statement of the fix.

    Under the leak this set was empty by construction: a discount that did not
    lead to a recovery was recorded as no discount at all.
    """
    if "discount_paise" not in frame.X.columns:
        pytest.skip("discount_paise is not in the frame")

    values = frame.X["discount_paise"].to_numpy(dtype=float, na_value=0.0)
    outcome = frame.outcome.to_numpy()
    offered_and_failed = int(((values > 0) & (outcome == 0)).sum())

    assert offered_and_failed > 0, (
        "every recorded discount coincides with a recovery — the column is "
        "still describing the payout rather than the offer"
    )
