"""When to re-present a mandate debit.

The "mandate retry sequencer" named on the track page. For an insufficient-funds
failure the question is not *whether* to retry but *when the customer will have
money*, and in India that is strongly periodic: salaries land at month end and
in the first days of the month.

The model is a hazard curve over day-of-month, learned from observed retry
outcomes rather than hard-coded. `calendar.balance_availability` in the
generator is the ground truth it must rediscover — it is never read here, and a
test asserts the learned curve correlates with it.

Regulatory limits are NOT expressed here. This module proposes a time; the
policy engine decides whether that time is permitted. Keeping the proposal
separate from the permission is what lets the console show "the model wanted
Tuesday, the RBI pre-debit rule moved it to Thursday".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import psycopg

from yukti.domain.decline import lookup
from yukti.domain.enums import Transience

# Mandate debits are presented in the morning batch. Proposing 03:00 would be
# meaningless: no issuer processes then.
DEBIT_HOURS = (7, 8, 9, 10)


@dataclass(frozen=True, slots=True)
class RetrySlot:
    """A proposed re-presentation time and why it was chosen."""

    at: datetime
    score: float           # relative success likelihood, 1.0 = best observed day
    reason: str

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M} (score {self.score:.2f})"


class DebitTimingModel:
    """Day-of-month hazard curve for funds-related retries."""

    def __init__(self) -> None:
        # Uniform until fitted, so an unfitted model proposes "as soon as
        # allowed" rather than silently preferring an arbitrary day.
        self.curve: np.ndarray = np.ones(32)
        self.fitted = False

    def fit(self, conn: psycopg.Connection) -> DebitTimingModel:
        """Learn success rate by day of month for funds-limited attempts.

        Only insufficient-funds attempts are used. Including system failures
        would blur the salary signal with issuer downtime, which follows a
        different and unrelated calendar.
        """
        rows = conn.execute(
            """
            SELECT extract(day FROM attempted_at)::int AS dom,
                   count(*)                              AS n,
                   count(*) FILTER (WHERE status = 'captured') AS ok
              FROM payment_attempt
             WHERE rail IN ('upi_autopay', 'enach', 'card_recurring')
               AND (decline_code IS NULL OR decline_code IN
                    ('INSUFFICIENT_FUNDS', 'AP01', 'EXCEEDS_LIMIT'))
             GROUP BY 1
             ORDER BY 1
            """
        ).fetchall()
        if not rows:
            return self

        curve = np.ones(32)
        total_n = sum(r["n"] for r in rows)
        base = sum(r["ok"] for r in rows) / max(1, total_n)

        for r in rows:
            n, ok = r["n"], r["ok"]
            if n < 5:
                continue
            # Shrink toward the base rate in proportion to how thin the day is.
            # Without this a day with three attempts and three successes would
            # outrank a day with four hundred attempts at a genuinely high rate.
            weight = n / (n + 30)
            rate = ok / n
            curve[r["dom"]] = (weight * rate + (1 - weight) * base) / max(base, 1e-6)

        self.curve = curve
        self.fitted = True
        return self

    def score_day(self, day_of_month: int) -> float:
        return float(self.curve[min(max(day_of_month, 1), 31)])

    def best_slot(
        self,
        after: datetime,
        decline_code: str | None,
        horizon_days: int = 35,
        min_gap_hours: int | None = None,
    ) -> RetrySlot:
        """Propose the best permitted-looking re-presentation time.

        `min_gap_hours` defaults to the decline code's own minimum. The policy
        engine may still push the result later; it will never pull it earlier.
        """
        spec = lookup(decline_code)
        gap = min_gap_hours if min_gap_hours is not None else spec.min_retry_gap_h
        earliest = after + timedelta(hours=gap)

        if spec.transience is Transience.PERMANENT:
            # Nothing to schedule. Returning a slot anyway would invite a caller
            # to act on it; the score of 0.0 says plainly that it is worthless.
            return RetrySlot(at=earliest, score=0.0,
                             reason=f"{spec.code} is permanent; no retry can succeed")

        best: RetrySlot | None = None
        for offset in range(horizon_days):
            day = earliest + timedelta(days=offset)
            score = self.score_day(day.day)
            if best is None or score > best.score:
                hour = DEBIT_HOURS[offset % len(DEBIT_HOURS)]
                at = day.replace(hour=hour, minute=0, second=0, microsecond=0)
                if at < earliest:
                    at = at + timedelta(days=1)
                best = RetrySlot(
                    at=at,
                    score=score,
                    reason=(
                        f"day {day.day} scores {score:.2f}x baseline for "
                        f"{spec.code or 'unknown'} — balance availability peaks "
                        "just after salary credit"
                    ),
                )
        assert best is not None
        return best

    def peak_days(self, n: int = 5) -> list[int]:
        """The n best days of the month, for diagnostics and the console."""
        return sorted(range(1, 32), key=lambda d: -self.curve[d])[:n]
