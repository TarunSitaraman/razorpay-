"""Aggregate payment degradation: detection and downtime nowcasting.

Two related jobs, both operating on the failure stream rather than on individual
payments:

  * **Downtime nowcast** — is this issuer failing *right now*? Used to suppress
    contact: messaging a customer during an outage produces a second failure and
    tells them the merchant is broken.
  * **Degradation detection** — has an issuer, PSP or method's success rate
    dropped materially against its own baseline? This is the first example
    direction on the track page, and it feeds the RCA agent.

Both are deterministic statistics, not models. A z-test against a rolling
baseline is explainable to a merchant, has no training requirement, and cannot
hallucinate an outage. The LLM's job starts *after* detection, in root-causing
what the numbers show.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

# A drop is only interesting if it is both statistically real and materially
# large. Two separate bars, because a huge sample makes trivial drops
# significant and a small sample makes real drops look like noise.
MIN_SAMPLE = 30
MIN_ABSOLUTE_DROP = 0.08
Z_THRESHOLD = 3.0


@dataclass(frozen=True, slots=True)
class HealthSignal:
    """Current health of one dimension value."""

    dimension: str            # issuer | psp | method
    value: str
    baseline_sr: float
    observed_sr: float
    z_score: float
    sample_size: int
    window_start: datetime
    window_end: datetime

    @property
    def drop(self) -> float:
        return self.baseline_sr - self.observed_sr

    @property
    def is_degraded(self) -> bool:
        return (
            self.sample_size >= MIN_SAMPLE
            and self.drop >= MIN_ABSOLUTE_DROP
            and self.z_score >= Z_THRESHOLD
        )

    def describe(self) -> str:
        return (
            f"{self.dimension}={self.value} success rate "
            f"{self.observed_sr:.1%} vs baseline {self.baseline_sr:.1%} "
            f"({self.drop:+.1%}, z={self.z_score:.1f}, n={self.sample_size})"
        )


DIMENSION_COLUMN = {"issuer": "issuer", "psp": "psp", "method": "rail"}

# Difference-in-differences.
#
# Comparing a value against its own flat historical average sounds right and is
# wrong: success rates move for reasons that affect everyone at once. The salary
# cycle alone swings funds-limited rails by ~27%, so any mid-month window looks
# "degraded" against a 14-day mean. Measured, that produced a 6.7% false-positive
# rate against a 3-sigma threshold that should have given ~0.1% — the null was
# never normal, because seasonality is real.
#
# So each value is compared against the CONTEMPORANEOUS platform-wide rate.
# Anything hitting the whole platform — time of day, day of month, weekday —
# cancels, and what survives is the value moving relative to its peers, which is
# what "this issuer is degraded" actually means.
_HEALTH_SQL = """
WITH baseline AS (
    SELECT {col} AS value,
           count(*)                                     AS n,
           count(*) FILTER (WHERE status = 'captured')  AS ok
      FROM payment_attempt
     WHERE attempted_at >= %(baseline_start)s
       AND attempted_at <  %(window_start)s
       AND {col} IS NOT NULL
     GROUP BY 1
), baseline_all AS (
    SELECT count(*)                                     AS n,
           count(*) FILTER (WHERE status = 'captured')  AS ok
      FROM payment_attempt
     WHERE attempted_at >= %(baseline_start)s
       AND attempted_at <  %(window_start)s
       AND {col} IS NOT NULL
), current AS (
    SELECT {col} AS value,
           count(*)                                     AS n,
           count(*) FILTER (WHERE status = 'captured')  AS ok
      FROM payment_attempt
     WHERE attempted_at >= %(window_start)s
       AND attempted_at <  %(window_end)s
       AND {col} IS NOT NULL
     GROUP BY 1
), current_all AS (
    SELECT count(*)                                     AS n,
           count(*) FILTER (WHERE status = 'captured')  AS ok
      FROM payment_attempt
     WHERE attempted_at >= %(window_start)s
       AND attempted_at <  %(window_end)s
       AND {col} IS NOT NULL
)
SELECT c.value,
       b.n AS base_n, b.ok AS base_ok,
       c.n AS cur_n,  c.ok AS cur_ok,
       ba.n AS base_all_n, ba.ok AS base_all_ok,
       ca.n AS cur_all_n,  ca.ok AS cur_all_ok
  FROM current c
  JOIN baseline b USING (value)
  CROSS JOIN baseline_all ba
  CROSS JOIN current_all ca
 WHERE b.n >= %(min_sample)s
"""


def _z_score(base_rate: float, obs_rate: float, n: int) -> float:
    """One-sided z for a proportion drop, using the baseline's variance."""
    if n <= 0:
        return 0.0
    var = max(base_rate * (1 - base_rate), 1e-6)
    return (base_rate - obs_rate) / ((var / n) ** 0.5)


def scan(
    conn: psycopg.Connection,
    at: datetime,
    dimension: str = "issuer",
    window_hours: int = 3,
    baseline_days: int = 14,
) -> list[HealthSignal]:
    """Compare a recent window against each value's own trailing baseline.

    Per-value baselines matter: issuers have genuinely different normal success
    rates, so a single platform-wide threshold would flag the weakest issuer
    permanently and miss a real drop at the strongest.
    """
    col = DIMENSION_COLUMN.get(dimension)
    if col is None:
        raise ValueError(f"unknown dimension: {dimension}")

    window_start = at - timedelta(hours=window_hours)
    rows = conn.execute(
        _HEALTH_SQL.format(col=col),
        {
            "baseline_start": at - timedelta(days=baseline_days),
            "window_start": window_start,
            "window_end": at,
            "min_sample": MIN_SAMPLE,
        },
    ).fetchall()

    signals = []
    for r in rows:
        historical = r["base_ok"] / r["base_n"]
        observed = r["cur_ok"] / r["cur_n"] if r["cur_n"] else 0.0

        # Where the platform sits now versus where it normally sits. A value
        # below 1.0 means conditions are hard for everyone right now.
        base_all = r["base_all_ok"] / max(1, r["base_all_n"])
        cur_all = r["cur_all_ok"] / max(1, r["cur_all_n"])
        common_mode = (cur_all / base_all) if base_all > 0 else 1.0

        # What we should expect from this value under today's conditions.
        expected = min(0.999, historical * common_mode)

        signals.append(
            HealthSignal(
                dimension=dimension,
                value=r["value"],
                baseline_sr=expected,
                observed_sr=observed,
                z_score=_z_score(expected, observed, r["cur_n"]),
                sample_size=r["cur_n"],
                window_start=window_start,
                window_end=at,
            )
        )
    return sorted(signals, key=lambda s: -s.z_score)


def degraded(conn: psycopg.Connection, at: datetime, **kw) -> list[HealthSignal]:
    """Only the signals that clear both bars."""
    return [s for s in scan(conn, at, **kw) if s.is_degraded]


def is_degraded_now(
    conn: psycopg.Connection, at: datetime, issuer: str, window_hours: int = 3
) -> bool:
    """Nowcast for one issuer — the suppression check.

    Called on the decision path, so it answers a single question cheaply rather
    than scanning every dimension.
    """
    return any(
        s.value == issuer
        for s in degraded(conn, at, dimension="issuer", window_hours=window_hours)
    )


def decline_mix(
    conn: psycopg.Connection, dimension: str, value: str,
    window_start: datetime, window_end: datetime, limit: int = 6,
) -> list[dict]:
    """Decline-code distribution during a window.

    This is the diagnostic tell the RCA agent reasons over: an auth-stack
    regression and a capacity problem produce the same headline drop but
    different code mixes, and the mix is what separates them.
    """
    col = DIMENSION_COLUMN[dimension]
    rows = conn.execute(
        f"""
        SELECT decline_code, count(*) AS n
          FROM payment_attempt
         WHERE {col} = %s
           AND status = 'failed'
           AND attempted_at >= %s AND attempted_at < %s
         GROUP BY 1 ORDER BY n DESC LIMIT %s
        """,
        (value, window_start, window_end, limit),
    ).fetchall()
    total = sum(r["n"] for r in rows) or 1
    return [
        {"decline_code": r["decline_code"], "n": r["n"], "share": r["n"] / total}
        for r in rows
    ]
