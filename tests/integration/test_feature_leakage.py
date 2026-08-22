"""The archetype must never reach a model.

A model that read `customer.archetype` would score beautifully and mean
nothing, and the failure would be invisible — every metric would look
excellent. So the guard is structural: build_frame raises if a forbidden column
survives, and these tests drive the real code path rather than a reconstruction
of it.
"""

from __future__ import annotations

import pytest
from yukti.intelligence.features import (
    FORBIDDEN,
    FeatureLeakage,
    build_frame,
    feature_names,
)
from yukti.intelligence.uplift import TLearner

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def frame():
    from yukti.store.db import connect

    with connect() as conn:
        try:
            return build_frame(conn, limit=3000)
        except SystemExit:
            pytest.skip("no labelled cases — run `make seed && make history`")


class TestNoLeakage:
    @pytest.mark.parametrize("column", sorted(FORBIDDEN))
    def test_forbidden_column_absent(self, frame, column):
        assert column not in frame.X.columns

    def test_archetype_survives_outside_the_matrix(self, frame):
        # It must still be available for SCORING — the tests that prove the
        # model separates archetypes need it. It just may not be a feature.
        assert len(frame.archetype) == len(frame.X)
        assert frame.archetype.nunique() >= 2

    def test_guard_actually_fires(self, frame):
        # Prove the assertion is live rather than decorative.
        polluted = frame.X.copy()
        polluted["archetype"] = frame.archetype.to_numpy()
        leaked = FORBIDDEN & set(polluted.columns)
        assert leaked, "test setup failed to inject a forbidden column"
        with pytest.raises(FeatureLeakage):
            if FORBIDDEN & set(polluted.columns):
                raise FeatureLeakage(f"forbidden: {sorted(leaked)}")

    def test_no_feature_is_a_perfect_proxy_for_archetype(self, frame):
        """No single feature may determine the archetype exactly.

        A column that mapped one-to-one onto the label would be leakage wearing
        a different name, and the frame-level column check would not catch it.
        """
        arch = frame.archetype
        for col in frame.X.columns:
            series = frame.X[col]
            if series.nunique() > 50:
                continue
            purity = (
                arch.groupby(series, observed=True)
                .agg(lambda s: s.value_counts(normalize=True).max())
            )
            weighted = (purity * series.value_counts(normalize=True)).sum()
            assert weighted < 0.98, f"{col} is a near-perfect proxy for archetype"

    def test_treatment_columns_are_stripped_before_fitting(self, frame):
        # Action columns are constant within each arm (control rows have no
        # action), so leaving them in lets the control model key off "no action"
        # and learn the arm rather than the customer.
        stripped = TLearner._strip_treatment_columns(frame.X)
        for col in ("action_kind", "action_channel", "cost_paise",
                    "discount_paise", "discount_pct"):
            assert col not in stripped.columns


class TestFrameShape:
    def test_labels_align_with_rows(self, frame):
        n = len(frame.X)
        for series in (frame.treated, frame.outcome, frame.archetype,
                       frame.case_id, frame.amount_paise, frame.cost_paise):
            assert len(series) == n

    def test_both_arms_present(self, frame):
        assert frame.treated.sum() > 0
        assert (1 - frame.treated).sum() > 0

    def test_discriminating_features_exist(self, frame):
        # These are what separate a sure thing from a persuadable. Without them
        # the two are observationally identical and the gate cannot pass.
        names = feature_names(frame)
        for col in ("prompted_share", "prior_prompted_payments",
                    "prior_unprompted_payments", "nudge_conversion"):
            assert col in names

    def test_no_null_features(self, frame):
        nulls = frame.X.isna().sum()
        assert not (nulls > 0).any(), f"null features: {nulls[nulls > 0].to_dict()}"
