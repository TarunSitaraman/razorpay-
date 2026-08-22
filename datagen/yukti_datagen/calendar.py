"""Time-varying environment effects for the Indian payments calendar.

These are the correlations that make the dataset learnable. A uniformly random
dataset would let a propensity model and an uplift model score identically,
which would make the whole evaluation vacuous.

Everything here is a *deterministic* function of (seed, timestamp, entity), so
a replay reproduces the same world exactly.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

IST_OFFSET_H = 5.5


def _h(seed: int, *parts: object) -> float:
    """Deterministic uniform in [0,1) from a seed and arbitrary parts."""
    key = "|".join([str(seed), *(str(p) for p in parts)])
    digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def balance_availability(day_of_month: int) -> float:
    """Probability multiplier that a retail customer's account has funds.

    Salaries in India land overwhelmingly at month end and the first few days of
    the month, so a debit that fails for insufficient funds on the 15th should
    be re-presented on the 1st rather than 24 hours later. This single curve is
    what the debit-timing model has to discover, and it is why a fixed +1d/+3d
    retry cadence leaves money on the table.

    Returns a multiplier centred near 1.0, peaking just after salary credit.
    """
    # Two peaks: 1st-7th (salary just landed) and 25th-31st (salary imminent for
    # some employers, and month-end disbursals).
    early = math.exp(-(((day_of_month - 3) / 3.0) ** 2))
    late = math.exp(-(((day_of_month - 28) / 3.0) ** 2)) * 0.75
    trough = 0.45  # mid-month floor
    return trough + (1.35 - trough) * min(1.0, early + late)


def hour_conversion_multiplier(hour_ist: int) -> float:
    """How responsive customers are to a contact, by hour of day (IST).

    Evening is when people actually act on a payment nudge. Note this curve is
    *not* clipped to the TRAI 09:00-21:00 window: the regulation is enforced
    separately by the policy engine. Keeping them separate is deliberate — it
    lets the evaluation show the agent wanting to message at 22:15 (locally
    optimal) and being blocked (globally correct).
    """
    curve = {
        0: 0.20, 1: 0.15, 2: 0.10, 3: 0.10, 4: 0.12, 5: 0.20,
        6: 0.40, 7: 0.60, 8: 0.75, 9: 0.85, 10: 0.95, 11: 1.00,
        12: 0.90, 13: 0.80, 14: 0.85, 15: 0.90, 16: 0.95, 17: 1.05,
        18: 1.20, 19: 1.30, 20: 1.25, 21: 1.00, 22: 0.70, 23: 0.40,
    }
    return curve[hour_ist % 24]


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


@dataclass(frozen=True, slots=True)
class DowntimeWindow:
    """A period where a specific issuer's success rate collapses."""

    issuer: str
    start: datetime
    end: datetime
    severity: float   # fraction of attempts that fail outright, 0..1

    def covers(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


def generate_downtime_windows(
    seed: int, issuers: list[str], start: datetime, days: int
) -> list[DowntimeWindow]:
    """Bursty, issuer-correlated downtime that clusters at month end.

    Downtime is the case where the correct recovery action is to do *nothing*
    and retry silently later. A system that messages customers during an issuer
    outage burns contact budget and teaches the customer the merchant is broken,
    so the dataset has to contain enough downtime for that mistake to be
    measurable.
    """
    windows: list[DowntimeWindow] = []
    for issuer in issuers:
        for day in range(days):
            d = start + timedelta(days=day)
            # Month end carries heavier batch load, so incidents cluster there.
            month_end_boost = 2.5 if d.day >= 27 or d.day <= 2 else 1.0
            p_incident = 0.035 * month_end_boost
            if _h(seed, "downtime", issuer, day) >= p_incident:
                continue
            start_h = int(_h(seed, "dt-hour", issuer, day) * 24)
            dur_h = 1 + int(_h(seed, "dt-dur", issuer, day) * 5)
            severity = 0.45 + _h(seed, "dt-sev", issuer, day) * 0.5
            ws = d.replace(hour=start_h, minute=0, second=0, microsecond=0)
            windows.append(DowntimeWindow(issuer, ws, ws + timedelta(hours=dur_h), severity))
    return sorted(windows, key=lambda w: w.start)


@dataclass(frozen=True, slots=True)
class DegradationEpisode:
    """A sustained success-rate drop on one dimension, with a known cause.

    This is the ground truth for "payment degradation -> root cause -> recovery
    action", the first example direction on the track page. The detector is
    scored against these, and the RCA specialist's narrative is checked against
    `true_cause`.
    """

    dimension: str        # issuer | psp | method
    value: str
    start: datetime
    end: datetime
    sr_drop: float        # absolute success-rate drop, e.g. 0.22
    true_cause: str       # the label the RCA agent should land on
    dominant_code: str    # decline code whose share spikes — the diagnostic tell

    def covers(self, ts: datetime) -> bool:
        return self.start <= ts < self.end


def generate_degradation_episodes(
    seed: int, issuers: list[str], psps: list[str], start: datetime, days: int
) -> list[DegradationEpisode]:
    """Inject a handful of multi-hour degradations with characteristic signatures.

    Each episode shifts the decline-code mix in a specific way, so root-causing
    it is a real inference from evidence rather than a lookup: an auth-stack
    regression looks different from a funds-side issue even when the headline
    success-rate drop is identical.
    """
    catalogue = [
        ("issuer_auth_regression", "AP39", 0.18, 0.30),
        ("issuer_capacity", "BANK_DOWN", 0.25, 0.45),
        ("psp_timeout_spike", "PSP_TIMEOUT", 0.20, 0.38),
        ("mandate_registry_sync", "AP12", 0.12, 0.22),
    ]
    episodes: list[DegradationEpisode] = []
    # ~1 episode every 6 days keeps them rare enough that false positives cost
    # the detector, but common enough to score.
    n = max(3, days // 6)
    for i in range(n):
        r = _h(seed, "deg", i)
        cause, code, lo, hi = catalogue[int(_h(seed, "deg-kind", i) * len(catalogue))]
        use_issuer = _h(seed, "deg-dim", i) < 0.65
        pool = issuers if use_issuer else psps
        value = pool[int(_h(seed, "deg-val", i) * len(pool))]
        day = int(r * days)
        hour = 6 + int(_h(seed, "deg-hour", i) * 12)
        dur = 3 + int(_h(seed, "deg-dur", i) * 9)
        ws = (start + timedelta(days=day)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        episodes.append(
            DegradationEpisode(
                dimension="issuer" if use_issuer else "psp",
                value=value,
                start=ws,
                end=ws + timedelta(hours=dur),
                sr_drop=lo + _h(seed, "deg-sev", i) * (hi - lo),
                true_cause=cause,
                dominant_code=code,
            )
        )
    return sorted(episodes, key=lambda e: e.start)
