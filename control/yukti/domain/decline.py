"""Decline-code taxonomy for Indian payment rails.

The mapping from a raw decline code to a recovery posture is the single most
important piece of payments domain knowledge in the system, so it lives in one
reviewable table rather than being scattered through the planner.

Codes follow the shapes published for NACH/e-mandate presentation and the
common UPI/card decline reasons. Anything not in this table is handed to the
LLM classifier and clamped to a ``Transience`` member; unknown never means
"treat aggressively".

``max_attempts`` encodes NPCI's per-reason re-presentation caps where a public
figure exists (AP39 / OTP-invalid permits 3). Where no cap is published we use a
conservative default rather than inventing a number, and the policy engine
treats the value as a hard ceiling regardless of what any model proposes.
"""

from __future__ import annotations

from dataclasses import dataclass

from yukti.domain.enums import Transience

# Conservative default when a rail permits re-presentation but no public
# per-reason cap is documented. Deliberately low: over-presenting a mandate is a
# compliance problem, under-presenting is only a revenue problem.
DEFAULT_MANDATE_MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class DeclineSpec:
    code: str
    label: str
    transience: Transience
    # Whether a retry on the same rail can plausibly succeed without the
    # customer doing anything.
    retryable_silently: bool
    # Whether contacting the customer can change the outcome. False for pure
    # system failures — contacting during issuer downtime burns a contact and
    # tells the customer the merchant is broken.
    customer_actionable: bool
    # Regulatory/practical ceiling on attempts for this reason.
    max_attempts: int
    # Earliest sensible re-attempt gap in hours. The debit-timing model may
    # choose *later* than this; it may never choose earlier.
    min_retry_gap_h: int


_SPECS: tuple[DeclineSpec, ...] = (
    # ---- Funds ---------------------------------------------------------------
    # The single largest recoverable bucket in India, and the one where *timing*
    # dominates: balance availability is strongly periodic around salary credit.
    DeclineSpec("INSUFFICIENT_FUNDS", "Insufficient balance",
                Transience.TRANSIENT_FUNDS, True, True, 4, 24),
    DeclineSpec("AP01", "Account closed / insufficient",
                Transience.TRANSIENT_FUNDS, True, True, 3, 24),
    DeclineSpec("EXCEEDS_LIMIT", "Per-txn or daily limit exceeded",
                Transience.TRANSIENT_FUNDS, True, True, 3, 24),

    # ---- System / issuer -----------------------------------------------------
    # Correct posture is to wait. Contacting the customer cannot help, and the
    # failure will often clear on its own within the hour.
    DeclineSpec("BANK_DOWN", "Issuer unavailable",
                Transience.TRANSIENT_SYSTEM, True, False, 5, 1),
    DeclineSpec("PSP_TIMEOUT", "PSP timeout",
                Transience.TRANSIENT_SYSTEM, True, False, 5, 1),
    DeclineSpec("GATEWAY_ERROR", "Gateway error",
                Transience.TRANSIENT_SYSTEM, True, False, 5, 1),
    DeclineSpec("NPCI_UNAVAILABLE", "NPCI switch unavailable",
                Transience.TRANSIENT_SYSTEM, True, False, 5, 1),

    # ---- Authentication ------------------------------------------------------
    # The customer can fix these immediately, so the right move is a prompt
    # nudge rather than a silent retry.
    DeclineSpec("AP39", "OTP invalid",
                Transience.TRANSIENT_AUTH, False, True, 3, 0),
    DeclineSpec("UPI_PIN_INCORRECT", "UPI PIN incorrect",
                Transience.TRANSIENT_AUTH, False, True, 3, 0),
    DeclineSpec("AUTH_TIMEOUT", "Customer did not authenticate in time",
                Transience.TRANSIENT_AUTH, False, True, 3, 0),
    DeclineSpec("COLLECT_EXPIRED", "UPI collect request expired",
                Transience.TRANSIENT_AUTH, False, True, 3, 0),

    # ---- Semi-permanent ------------------------------------------------------
    # Needs the customer to change something. No number of silent retries helps.
    DeclineSpec("CARD_EXPIRED", "Card expired",
                Transience.SEMI_PERMANENT, False, True, 1, 0),
    DeclineSpec("MANDATE_PAUSED", "Mandate paused by customer",
                Transience.SEMI_PERMANENT, False, True, 1, 0),
    DeclineSpec("AP12", "Mandate not registered at bank",
                Transience.SEMI_PERMANENT, False, True, 1, 0),

    # ---- Permanent -----------------------------------------------------------
    # Every rupee spent chasing these is pure loss. Detecting them is worth as
    # much as recovering a persuadable.
    DeclineSpec("MANDATE_REVOKED", "Mandate cancelled",
                Transience.PERMANENT, False, False, 0, 0),
    DeclineSpec("ACCOUNT_CLOSED", "Account closed",
                Transience.PERMANENT, False, False, 0, 0),
    DeclineSpec("CARD_BLOCKED", "Card blocked or reported lost",
                Transience.PERMANENT, False, False, 0, 0),
    DeclineSpec("AP08", "Account frozen",
                Transience.PERMANENT, False, False, 0, 0),
)

BY_CODE: dict[str, DeclineSpec] = {s.code: s for s in _SPECS}

# Returned for codes absent from the table. Permits exactly one cheap attempt,
# nothing more, until the classifier says otherwise.
UNKNOWN = DeclineSpec(
    code="UNKNOWN",
    label="Unrecognised decline reason",
    transience=Transience.UNCLASSIFIED,
    retryable_silently=True,
    customer_actionable=True,
    max_attempts=1,
    min_retry_gap_h=24,
)


def lookup(code: str | None) -> DeclineSpec:
    """Resolve a decline code, falling back to the conservative UNKNOWN spec."""
    if not code:
        return UNKNOWN
    return BY_CODE.get(code.strip().upper(), UNKNOWN)


def all_codes() -> tuple[str, ...]:
    return tuple(BY_CODE)
