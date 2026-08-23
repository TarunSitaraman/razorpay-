"""Closed enumerations for the Yukti domain.

These matter more than usual here. Anywhere the LLM produces a classification,
its output is clamped to one of these enums and an unrecognised value degrades
to the conservative member rather than propagating. A free-text label from a
model must never reach a decision, a policy check or a database column.
"""

from __future__ import annotations

from enum import StrEnum


class ObligationKind(StrEnum):
    """The four revenue-loss surfaces named in the track brief.

    Modelling them as one abstraction is the load-bearing design decision: the
    cross-surface arbitration gap exists precisely because today these are four
    separate products with four separate contact budgets.
    """

    CART = "cart"                            # checkout abandonment
    SUBSCRIPTION_CYCLE = "subscription_cycle"  # failed recurring charge
    INVOICE = "invoice"                      # B2B overdue receivable
    ORDER = "order"                          # one-off failed payment


class ObligationState(StrEnum):
    OPEN = "open"
    RECOVERED = "recovered"
    LOST = "lost"
    EXPIRED = "expired"


class Rail(StrEnum):
    """Payment rails, with India's specifics kept distinct.

    UPI intent and UPI collect are separate members because their success rates
    differ materially and the recovery strategy differs with them; collapsing
    them into "UPI" would destroy the signal.
    """

    UPI_INTENT = "upi_intent"
    UPI_COLLECT = "upi_collect"
    UPI_AUTOPAY = "upi_autopay"    # e-mandate on UPI
    CARD = "card"
    CARD_RECURRING = "card_recurring"
    NETBANKING = "netbanking"
    ENACH = "enach"                # bank mandate debit
    WALLET = "wallet"

    @property
    def is_mandate(self) -> bool:
        """Mandate rails are the ones bound by RBI pre-debit and NPCI caps."""
        return self in {Rail.UPI_AUTOPAY, Rail.ENACH, Rail.CARD_RECURRING}

    @property
    def is_customer_initiated(self) -> bool:
        """Rails where recovery requires the customer to act."""
        return self in {
            Rail.UPI_INTENT, Rail.UPI_COLLECT, Rail.CARD,
            Rail.NETBANKING, Rail.WALLET,
        }


class Transience(StrEnum):
    """How likely a failure is to resolve on its own.

    This taxonomy is the bridge between a raw decline code and a recovery
    posture, and it is the one classification the LLM is allowed to make for
    unseen codes. ``UNCLASSIFIED`` is the conservative default: it routes to the
    cheapest possible action, never to a discount or a voice call.
    """

    TRANSIENT_SYSTEM = "transient_system"      # bank/PSP down — wait, then silent retry
    TRANSIENT_FUNDS = "transient_funds"        # insufficient funds — retry when balance likely
    TRANSIENT_AUTH = "transient_auth"          # OTP/PIN failure — customer can retry now
    SEMI_PERMANENT = "semi_permanent"          # card expiring, mandate paused — needs customer action
    PERMANENT = "permanent"                    # revoked mandate, blocked card, closed account
    UNCLASSIFIED = "unclassified"              # unknown code — treat conservatively


class ActionKind(StrEnum):
    """The complete set of things Yukti may do about an obligation.

    This is deliberately short. Refunds, payouts, settlement changes and mandate
    cancellation are absent — not denied at runtime, absent — so that neither the
    planner nor a prompt-injected instruction can name them.
    """

    SUPPRESS = "suppress"                # decide to do nothing, and record why
    SILENT_RETRY = "silent_retry"        # re-attempt without contacting the customer
    SCHEDULE_DEBIT = "schedule_debit"    # schedule a mandate debit at a chosen time
    PAYMENT_LINK = "payment_link"        # generate a link (delivery is a separate action)
    MESSAGE = "message"                  # WhatsApp / SMS / email, DLT-template-bound
    VOICE_CALL = "voice_call"            # simulated in this build
    DISCOUNT_OFFER = "discount_offer"    # incentive, always paired with a delivery channel
    ESCALATE = "escalate"                # hand to a human; never an autonomous action

    @property
    def contacts_customer(self) -> bool:
        """Whether this action spends from the customer's contact budget."""
        return self in {ActionKind.MESSAGE, ActionKind.VOICE_CALL, ActionKind.DISCOUNT_OFFER}

    @property
    def moves_money(self) -> bool:
        """Whether this action can cause a debit attempt against the customer."""
        return self in {ActionKind.SILENT_RETRY, ActionKind.SCHEDULE_DEBIT}


class Channel(StrEnum):
    NONE = "none"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    EMAIL = "email"
    VOICE = "voice"


class CaseState(StrEnum):
    """Lifecycle of a recovery case.

    ``STOPPED`` is distinct from ``LOST``: stopped means Yukti chose to cease
    work under a named stopping rule while the obligation may still resolve on
    its own. Collapsing the two would make the stopping-rules metric unreadable.
    """

    OPEN = "open"
    PLANNING = "planning"
    SCHEDULED = "scheduled"
    ACTING = "acting"
    AWAITING_OUTCOME = "awaiting_outcome"
    # Held for a human. Distinct from STOPPED because the agent has not decided
    # not to act — it has decided it is not the one who gets to decide. The
    # money is still in play and the merchant still owes themselves an answer,
    # which is a different line on the console from "we chose to walk away".
    ESCALATED = "escalated"
    STOPPED = "stopped"
    RECOVERED = "recovered"
    LOST = "lost"

    @property
    def is_terminal(self) -> bool:
        return self in {CaseState.STOPPED, CaseState.RECOVERED, CaseState.LOST}


class StopReason(StrEnum):
    """Named stopping rules.

    The track brief asks for "stopping rules" explicitly, so every stop carries
    one of these IDs and the console groups by it. A stop with no reason is a
    bug, not a default.
    """

    LOST_CAUSE = "lost_cause"                          # permanent failure or predicted-zero uplift
    OPEN_PROMISE_TO_PAY = "open_promise_to_pay"        # customer promised; chasing lowers recovery
    CONTACT_BUDGET_SPENT = "contact_budget_spent"
    DISCOUNT_BUDGET_SPENT = "discount_budget_spent"
    CUSTOMER_OPTED_OUT = "customer_opted_out"
    NPCI_REPRESENT_CAP = "npci_represent_cap"          # regulatory attempt cap reached
    ISSUER_DEGRADED = "issuer_degraded"                # suppress during a degradation window
    DIMINISHING_RETURNS = "diminishing_returns"        # past the recovery-curve knee
    NEGATIVE_EXPECTED_MARGIN = "negative_expected_margin"
    OBLIGATION_RESOLVED = "obligation_resolved"


class PolicyVerdict(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    ESCALATE = "escalate"   # above a merchant approval threshold — a human decides


class Arm(StrEnum):
    """Experiment arm.

    ``HOLDOUT`` receives no recovery action at all. It is the only way to know
    what organic recovery looks like, and therefore the only honest denominator
    for an incremental-lift claim.
    """

    HOLDOUT = "holdout"
    TREATMENT = "treatment"


class PromiseState(StrEnum):
    OPEN = "open"
    KEPT = "kept"
    BROKEN = "broken"
    SUPERSEDED = "superseded"


class UpliftArchetype(StrEnum):
    """Ground-truth customer archetypes, used by the generator and the scorer.

    This is never a model feature. It exists so the synthetic data has a real
    causal structure and so evaluation can report *why* an arm lost money, not
    just that it did.
    """

    SURE_THING = "sure_thing"        # recovers anyway; treating wastes spend
    PERSUADABLE = "persuadable"      # recovers only if treated correctly
    LOST_CAUSE = "lost_cause"        # never recovers; treating wastes spend
    SLEEPING_DOG = "sleeping_dog"    # recovers if left alone; churns if over-contacted


class ActorKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"
