"""Debit timing, degradation detection, and the payment-intelligence seam.

All deterministic — no LLM, no network. These are the parts of the intelligence
layer that must be explainable to a merchant and cannot be allowed to
hallucinate an outage.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

import pytest
from yukti.config import settings
from yukti.domain.enums import Rail
from yukti.intelligence.debit_timing import DebitTimingModel
from yukti.intelligence.degradation import (
    MIN_ABSOLUTE_DROP,
    Z_THRESHOLD,
    decline_mix,
    scan,
)
from yukti.intelligence.payment_intel import (
    NullProvider,
    PaymentIntelligenceProvider,
    SimulatedProvider,
    TransactionScore,
    default_provider,
)
from yukti.store.db import connect
from yukti_datagen.calendar import balance_availability, generate_downtime_windows
from yukti_datagen.world import ISSUERS

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db():
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM payment_attempt").fetchone()["n"]
        if n < 5000:
            pytest.skip("run `make seed` first")
        yield conn


@pytest.fixture(scope="module")
def timing(db):
    return DebitTimingModel().fit(db)


class TestDebitTiming:
    def test_model_fits(self, timing):
        assert timing.fitted

    def test_rediscovers_the_salary_cycle(self, timing):
        """The generator's balance_availability curve is never read by the model.

        It must find the pattern from observed outcomes alone, which is the
        whole point — a hard-coded curve would prove nothing.
        """
        peaks = timing.peak_days(6)
        # Salaries land at month end and the first days of the month.
        assert any(d <= 5 for d in peaks), f"no early-month peak in {peaks}"
        assert all(7 <= d <= 24 for d in peaks) is False

    def test_learned_curve_correlates_with_ground_truth(self, timing):
        import numpy as np

        learned = np.array([timing.score_day(d) for d in range(1, 32)])
        truth = np.array([balance_availability(d) for d in range(1, 32)])
        r = float(np.corrcoef(learned, truth)[0, 1])
        assert r > 0.5, f"learned curve does not track balance availability (r={r:.2f})"

    def test_mid_month_failure_is_retried_after_salary(self, timing):
        slot = timing.best_slot(datetime(2026, 5, 15, 9, 0), "INSUFFICIENT_FUNDS")
        # A fixed +1d/+3d cadence would retry on the 16th, into the trough.
        assert slot.at.day <= 5 or slot.at.day >= 27
        assert slot.at > datetime(2026, 5, 15, 9, 0)

    def test_permanent_failures_get_a_worthless_score(self, timing):
        slot = timing.best_slot(datetime(2026, 5, 15, 9, 0), "MANDATE_REVOKED")
        assert slot.score == 0.0
        assert "permanent" in slot.reason

    def test_minimum_gap_is_respected(self, timing):
        after = datetime(2026, 5, 15, 9, 0)
        slot = timing.best_slot(after, "INSUFFICIENT_FUNDS", min_gap_hours=48)
        assert slot.at >= after + timedelta(hours=48)

    def test_proposed_hours_are_plausible_for_a_debit_batch(self, timing):
        for day in (1, 10, 20, 28):
            slot = timing.best_slot(datetime(2026, 5, day, 9, 0), "INSUFFICIENT_FUNDS")
            assert 6 <= slot.at.hour <= 11, "mandate debits do not present overnight"

    def test_unfitted_model_is_neutral_not_arbitrary(self):
        blank = DebitTimingModel()
        assert not blank.fitted
        scores = {blank.score_day(d) for d in range(1, 32)}
        # Uniform: proposes "as soon as allowed" rather than a made-up day.
        assert scores == {1.0}


class TestDegradationDetection:
    @pytest.fixture(scope="class")
    def episodes(self, db):
        rows = db.execute(
            "SELECT dimension, dimension_value, window_start, window_end, injected_truth "
            "FROM degradation_signal WHERE state = 'ground_truth' ORDER BY window_start"
        ).fetchall()
        if not rows:
            pytest.skip("no injected episodes — run `make seed`")
        return rows

    def test_detects_most_injected_episodes(self, db, episodes):
        hits = 0
        for e in episodes:
            hours = int((e["window_end"] - e["window_start"]).total_seconds() / 3600) + 1
            found = [
                s for s in scan(db, e["window_end"], dimension=e["dimension"],
                                window_hours=hours)
                if s.value == e["dimension_value"] and s.is_degraded
            ]
            hits += bool(found)
        # Misses are the marginal drops, where abstaining beats firing on noise.
        assert hits >= len(episodes) * 0.6, f"recall {hits}/{len(episodes)}"

    def test_false_positive_rate_is_low(self, db, episodes):
        """Difference-in-differences keeps seasonality out of the alarm.

        Against a flat historical baseline this measured 6.68% — the salary
        cycle alone makes any mid-month window look degraded. Comparing against
        the contemporaneous platform rate cancels the common mode.
        """
        lo, hi = db.execute(
            "SELECT min(attempted_at) a, max(attempted_at) b FROM payment_attempt"
        ).fetchone().values()
        downtime = generate_downtime_windows(
            settings().seed, ISSUERS, lo.replace(tzinfo=None), 61
        )

        def genuinely_degraded(dim: str, val: str, t) -> bool:
            naive = t.replace(tzinfo=None)
            if any(
                e["dimension"] == dim and e["dimension_value"] == val
                and e["window_start"] - timedelta(hours=6) <= t <= e["window_end"] + timedelta(hours=6)
                for e in episodes
            ):
                return True
            # An issuer outage is a real degradation, not a false alarm.
            return dim == "issuer" and any(
                w.issuer == val
                and w.start - timedelta(hours=6) <= naive <= w.end + timedelta(hours=6)
                for w in downtime
            )

        span = (hi - lo).total_seconds()
        rng = random.Random(7)
        checked = false_positives = 0
        for _ in range(60):
            t = lo + timedelta(seconds=rng.random() * span)
            if t < lo + timedelta(days=15):
                continue                    # need baseline history
            for dim in ("issuer", "psp"):
                for s in scan(db, t, dimension=dim, window_hours=4):
                    if genuinely_degraded(dim, s.value, t):
                        continue
                    checked += 1
                    false_positives += s.is_degraded

        assert checked > 100, "not enough clean windows to measure"
        assert false_positives / checked < 0.05

    def test_both_bars_must_be_cleared(self, db):
        for s in scan(db, db.execute(
            "SELECT max(attempted_at) m FROM payment_attempt").fetchone()["m"]):
            if s.is_degraded:
                # Statistical significance alone is not enough: a huge sample
                # makes a trivial drop significant.
                assert s.drop >= MIN_ABSOLUTE_DROP
                assert s.z_score >= Z_THRESHOLD

    def test_unknown_dimension_is_rejected(self, db):
        with pytest.raises(ValueError):
            scan(db, datetime(2026, 5, 15), dimension="not_a_dimension")

    def test_decline_mix_gives_the_diagnostic_tell(self, db, episodes):
        """An auth regression and a capacity problem look identical in the
        headline drop and different in the decline-code mix. That mix is what
        the RCA agent reasons over."""
        e = episodes[0]
        mix = decline_mix(db, e["dimension"], e["dimension_value"],
                          e["window_start"], e["window_end"])
        assert mix
        assert abs(sum(m["share"] for m in mix) - 1.0) < 0.01
        truth = e["injected_truth"] if isinstance(e["injected_truth"], dict) \
            else json.loads(e["injected_truth"])
        codes = {m["decline_code"] for m in mix}
        assert truth["dominant_code"] in codes


class TestPaymentIntelligenceSeam:
    def test_simulated_provider_satisfies_the_protocol(self):
        assert isinstance(SimulatedProvider(), PaymentIntelligenceProvider)
        assert isinstance(NullProvider(), PaymentIntelligenceProvider)

    def test_default_provider_is_labelled_simulated(self):
        # It must be impossible for a demo screenshot to imply a real model
        # produced the number.
        assert default_provider().name == "simulated"

    def test_every_score_carries_its_provenance(self):
        s = default_provider().score(
            rail=Rail.UPI_AUTOPAY, issuer="HDFC", amount_paise=250_000,
            decline_code="INSUFFICIENT_FUNDS", at=datetime(2026, 5, 15),
        )
        assert s.provider == "simulated"

    def test_permanent_decline_scores_zero(self):
        s = default_provider().score(
            rail=Rail.ENACH, issuer="SBI", amount_paise=250_000,
            decline_code="MANDATE_REVOKED", at=datetime(2026, 5, 15),
        )
        assert s.success_probability == 0.0
        assert s.best_rail is None

    def test_collect_is_routed_to_intent(self):
        # A real and well-documented asymmetry on Indian rails.
        s = default_provider().score(
            rail=Rail.UPI_COLLECT, issuer="HDFC", amount_paise=100_000,
            decline_code=None, at=datetime(2026, 5, 15),
        )
        assert s.best_rail is Rail.UPI_INTENT

    def test_null_provider_is_visibly_uninformed(self):
        # A consumer that forgets to check availability should make an obviously
        # uninformed decision, not a confidently wrong one.
        s = NullProvider().score()
        assert s.success_probability == 0.5 and s.risk_score == 0.5
        assert s.provider == "null"

    @pytest.mark.parametrize("bad", [-0.1, 1.5])
    def test_scores_out_of_range_are_rejected(self, bad):
        with pytest.raises(ValueError):
            TransactionScore(success_probability=bad, best_rail=None,
                             risk_score=0.1, provider="x")
