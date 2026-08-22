"""Payment intelligence: the seam where transaction-level scoring plugs in.

Razorpay's Vulcan is a payments foundation model that scores roughly 3,000
signals per transaction for routing, fraud, RTO risk and checkout
personalisation. It answers *"how should this one transaction be attempted right
now?"*

Yukti answers a different question: *"across every open case this week, which
multi-day sequences should I fund under a discount budget, TRAI messaging hours
and NPCI re-presentation caps?"* Those compose rather than compete — transaction
intelligence is an input to portfolio allocation.

This module is the interface that keeps that boundary explicit. The
implementation here is a **simulated stand-in**, built from the same synthetic
world as everything else. It is NOT Vulcan, is never described as Vulcan, and
produces no output that should be read as a Vulcan score. Its purpose is to
prove the seam exists and that swapping in a real provider is a config change
rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from yukti.domain.decline import lookup
from yukti.domain.enums import Rail, Transience


@dataclass(frozen=True, slots=True)
class TransactionScore:
    """Per-attempt intelligence, whatever the provider.

    Deliberately narrow. A provider returning a hundred fields would tempt the
    allocator into depending on one vendor's schema; these four are what a
    portfolio decision actually needs.
    """

    success_probability: float     # P(this attempt succeeds, as configured)
    best_rail: Rail | None         # rail the provider would route to
    risk_score: float              # 0 = benign, 1 = high risk
    provider: str                  # who produced this, for the audit trail

    def __post_init__(self) -> None:
        if not 0.0 <= self.success_probability <= 1.0:
            raise ValueError("success_probability out of range")
        if not 0.0 <= self.risk_score <= 1.0:
            raise ValueError("risk_score out of range")


@runtime_checkable
class PaymentIntelligenceProvider(Protocol):
    """What Yukti needs from a transaction-scoring service."""

    name: str

    def score(
        self,
        *,
        rail: Rail,
        issuer: str | None,
        amount_paise: int,
        decline_code: str | None,
        at: datetime,
    ) -> TransactionScore:
        ...


class SimulatedProvider:
    """Stand-in built from the local synthetic world.

    Explicitly a simulator. Named so that it appears as
    `provider="simulated"` in every audit record, which makes it impossible for
    a demo screenshot to imply a real model produced the number.
    """

    name = "simulated"

    # Base success rates per rail, mirroring the generator's own assumptions.
    # UPI intent genuinely outperforms UPI collect on Indian rails.
    _BASE: dict[Rail, float] = {
        Rail.UPI_INTENT: 0.94,
        Rail.UPI_COLLECT: 0.76,
        Rail.UPI_AUTOPAY: 0.88,
        Rail.CARD: 0.87,
        Rail.CARD_RECURRING: 0.82,
        Rail.NETBANKING: 0.85,
        Rail.ENACH: 0.80,
        Rail.WALLET: 0.93,
    }

    def score(
        self,
        *,
        rail: Rail,
        issuer: str | None,
        amount_paise: int,
        decline_code: str | None,
        at: datetime,
    ) -> TransactionScore:
        spec = lookup(decline_code)
        p = self._BASE.get(rail, 0.85)

        # A prior failure is informative about the next attempt.
        if spec.transience is Transience.PERMANENT:
            p = 0.0
        elif spec.transience is Transience.SEMI_PERMANENT:
            p *= 0.20
        elif spec.transience is Transience.TRANSIENT_SYSTEM:
            p *= 0.55
        elif spec.transience is Transience.TRANSIENT_FUNDS:
            p *= 0.70
        elif spec.transience is Transience.TRANSIENT_AUTH:
            p *= 0.80

        # High-value attempts face more scrutiny and more step-up friction.
        if amount_paise > 50_000_00:
            p *= 0.92

        best = self._best_rail(rail, spec.transience)
        risk = 0.35 if amount_paise > 100_000_00 else 0.08

        return TransactionScore(
            success_probability=max(0.0, min(1.0, p)),
            best_rail=best,
            risk_score=risk,
            provider=self.name,
        )

    @staticmethod
    def _best_rail(current: Rail, transience: Transience) -> Rail | None:
        """Suggest a switch only where one plausibly helps."""
        if transience is Transience.PERMANENT:
            return None
        # Collect is materially weaker than intent, so a customer-initiated
        # retry should prefer intent.
        if current is Rail.UPI_COLLECT:
            return Rail.UPI_INTENT
        return current


class NullProvider:
    """No intelligence available.

    Returned when a provider is unreachable. It reports an explicitly neutral
    score rather than a plausible-looking guess, so a downstream consumer that
    forgets to check availability makes a visibly uninformed decision instead of
    a confidently wrong one.
    """

    name = "null"

    def score(self, **_: object) -> TransactionScore:
        return TransactionScore(
            success_probability=0.5,
            best_rail=None,
            risk_score=0.5,
            provider=self.name,
        )


def default_provider() -> PaymentIntelligenceProvider:
    """The provider Yukti uses locally.

    A real deployment would return a client for a hosted scoring service here.
    Nothing else in the codebase changes.
    """
    return SimulatedProvider()
