"""Stopping rules — deciding which cases not to work at all.

The track brief names "stopping rules" explicitly, so they are a named module
with one function per rule rather than an emergent property of the allocator.
Every stop carries a `StopReason`, the console groups by it, and a stop with no
reason is a bug rather than a default.

The distinction from the policy engine matters and is deliberate:

    StoppingRules  "should we work this case at all?"     — a business decision
    PolicyEngine   "is this action permitted right now?"  — a compliance decision

Collapsing them would make both the console and the audit trail unreadable: a
merchant needs to see "we chose not to chase this" separately from "we were not
allowed to do this".

Ordering inside `evaluate` is by cost, cheapest first. A case that is already
resolved should not cost a model inference to reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from yukti.domain.decline import lookup
from yukti.domain.enums import ObligationState, StopReason, Transience

# Past this many days from the first failure, recovery attempts stop paying for
# themselves. Industry dunning data puts most recoveries inside two weeks, and
# days 21-30 mostly buy fatigue.
DIMINISHING_RETURNS_DAYS = 21

# Expected incremental margin below which acting is not worth the cost. Not
# zero: a decision that clears zero by a rupee is inside the model's noise, and
# spending on it burns contact budget that a clearer case could use.
MIN_EXPECTED_MARGIN_PAISE = 500

# Uplift below which a case is treated as unrecoverable regardless of decline
# code. Catches customers the model has learned are lost even when the code
# looks retryable.
LOST_CAUSE_UPLIFT = 0.005


@dataclass(frozen=True, slots=True)
class CaseSnapshot:
    """Everything a stopping rule needs. No I/O inside the rules themselves.

    Assembled once by the caller so the rules stay pure and exhaustively
    testable — every branch here decides whether real money gets spent.
    """

    case_id: str
    obligation_state: ObligationState
    decline_code: str | None
    first_failed_at: datetime
    attempts_made: int
    customer_opted_out: bool
    open_promise_to_pay: bool
    issuer_degraded: bool
    contacts_this_window: int
    contact_cap: int
    contact_budget_remaining: int
    discount_budget_remaining_paise: int
    predicted_uplift: float
    expected_margin_paise: int
    requires_discount: bool = False
    # Whether a costless, never-seen action (a silent retry) remains on the
    # table for this case. See `negative_expected_margin` for why it matters.
    has_costless_action: bool = False


@dataclass(frozen=True, slots=True)
class StopDecision:
    """Whether to stop, and under which named rule."""

    stop: bool
    reason: StopReason | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        # The invariant the brief cares about: a stop always names its rule.
        if self.stop and self.reason is None:
            raise ValueError("a stop must carry a named StopReason")
        if not self.stop and self.reason is not None:
            raise ValueError("a continue must not carry a StopReason")


CONTINUE = StopDecision(stop=False)


def _stop(reason: StopReason, detail: str) -> StopDecision:
    return StopDecision(stop=True, reason=reason, detail=detail)


# --- individual rules -------------------------------------------------------
# Each returns a StopDecision so they can be tested and reasoned about alone.

def obligation_resolved(s: CaseSnapshot) -> StopDecision:
    if s.obligation_state is not ObligationState.OPEN:
        return _stop(StopReason.OBLIGATION_RESOLVED,
                     f"obligation is {s.obligation_state.value}")
    return CONTINUE


def customer_opted_out(s: CaseSnapshot) -> StopDecision:
    """Opt-out is global and immediate, across every agent and surface.

    DPDP treats withdrawal of consent as binding, and a per-agent opt-out that
    another agent can ignore is not an opt-out.
    """
    if s.customer_opted_out:
        return _stop(StopReason.CUSTOMER_OPTED_OUT,
                     "customer has withdrawn consent; no contact on any channel")
    return CONTINUE


def open_promise_to_pay(s: CaseSnapshot) -> StopDecision:
    """An unbroken promise is a reason to go quiet, not a reason to follow up.

    Measured in the outcome oracle: chasing through an open promise is
    net-negative, because the promise already captures most of the recoverable
    probability and chasing risks it.
    """
    if s.open_promise_to_pay:
        return _stop(StopReason.OPEN_PROMISE_TO_PAY,
                     "customer has an open promise to pay; chasing lowers recovery")
    return CONTINUE


def lost_cause(s: CaseSnapshot) -> StopDecision:
    """Permanent decline, or a model-predicted near-zero causal effect."""
    spec = lookup(s.decline_code)
    if spec.transience is Transience.PERMANENT:
        return _stop(StopReason.LOST_CAUSE,
                     f"{spec.code} is permanent; no action can recover this")
    if s.predicted_uplift < LOST_CAUSE_UPLIFT and not s.has_costless_action:
        return _stop(StopReason.LOST_CAUSE,
                     f"predicted uplift {s.predicted_uplift:+.4f} is indistinguishable "
                     "from zero")
    # A near-zero ESTIMATE on a costless, unseen action is not evidence the case
    # is unrecoverable — it is evidence the estimator cannot resolve an effect
    # that small. Telling a merchant their book is a lost cause on that basis
    # reports an operational limit as a business fact, and it costs the free
    # retry that would sometimes have worked. The permanent-decline branch above
    # is deliberately NOT guarded: there a retry really is worthless (the oracle
    # scores it at exactly zero) and it would burn an NPCI attempt for nothing.
    return CONTINUE


def npci_cap_reached(s: CaseSnapshot) -> StopDecision:
    """Per-reason re-presentation ceiling.

    Sourced from `domain.decline` so the classifier, the guardrails and this
    rule cannot drift apart — a code means one thing everywhere.
    """
    spec = lookup(s.decline_code)
    if s.attempts_made >= spec.max_attempts:
        return _stop(StopReason.NPCI_REPRESENT_CAP,
                     f"{s.attempts_made} attempts made; {spec.code} permits "
                     f"{spec.max_attempts}")
    return CONTINUE


def issuer_degraded(s: CaseSnapshot) -> StopDecision:
    """Suppress while the issuer is failing.

    Not a permanent stop — this case should be reconsidered once the outage
    clears. Acting now produces a second failure and teaches the customer the
    merchant is broken.
    """
    if s.issuer_degraded:
        return _stop(StopReason.ISSUER_DEGRADED,
                     "issuer is degraded right now; wait rather than burn an attempt")
    return CONTINUE


def diminishing_returns(s: CaseSnapshot, now: datetime) -> StopDecision:
    age_days = (now - s.first_failed_at).days
    if age_days >= DIMINISHING_RETURNS_DAYS:
        return _stop(StopReason.DIMINISHING_RETURNS,
                     f"{age_days} days since first failure; past the recovery curve knee")
    return CONTINUE


def contact_budget_spent(s: CaseSnapshot) -> StopDecision:
    """Both the customer's own cap and the merchant's daily pool.

    The per-customer cap is the cross-agent arbitration constraint: it counts
    contacts from every surface, which is precisely what a per-agent budget
    cannot see.
    """
    if s.contacts_this_window >= s.contact_cap:
        return _stop(StopReason.CONTACT_BUDGET_SPENT,
                     f"customer has had {s.contacts_this_window} contacts this window "
                     f"(cap {s.contact_cap}) across all surfaces")
    if s.contact_budget_remaining <= 0:
        return _stop(StopReason.CONTACT_BUDGET_SPENT,
                     "merchant daily contact budget is exhausted")
    return CONTINUE


def discount_budget_spent(s: CaseSnapshot) -> StopDecision:
    if s.requires_discount and s.discount_budget_remaining_paise <= 0:
        return _stop(StopReason.DISCOUNT_BUDGET_SPENT,
                     "merchant daily discount budget is exhausted")
    return CONTINUE


def negative_expected_margin(s: CaseSnapshot) -> StopDecision:
    if s.has_costless_action:
        # A silent retry costs nothing and the customer never sees it, so it has
        # no downside to weigh against — its true effect is small but never
        # negative. That makes the margin test on it a test of the *estimate's*
        # sign, which near zero is close to a coin flip. Stopping the case here
        # would throw away a free option on estimator noise, so the case stays
        # open and the allocator funds the retry.
        return CONTINUE
    if s.expected_margin_paise < MIN_EXPECTED_MARGIN_PAISE:
        return _stop(StopReason.NEGATIVE_EXPECTED_MARGIN,
                     f"expected incremental margin {s.expected_margin_paise} paise is "
                     f"below the {MIN_EXPECTED_MARGIN_PAISE} paise floor")
    return CONTINUE


# --- composition ------------------------------------------------------------

def evaluate(snapshot: CaseSnapshot, now: datetime) -> StopDecision:
    """Apply every rule in cost order and return the first stop.

    First-match wins so the reported reason is the most fundamental one. A case
    on a revoked mandate whose budget is also exhausted should read LOST_CAUSE,
    not CONTACT_BUDGET_SPENT — the merchant needs to know the money is gone, not
    that we ran out of messages.
    """
    for rule in (
        obligation_resolved,      # free
        customer_opted_out,       # free, and legally binding
        open_promise_to_pay,
        lost_cause,
        npci_cap_reached,
        issuer_degraded,
    ):
        decision = rule(snapshot)
        if decision.stop:
            return decision

    decision = diminishing_returns(snapshot, now)
    if decision.stop:
        return decision

    for rule in (contact_budget_spent, discount_budget_spent, negative_expected_margin):
        decision = rule(snapshot)
        if decision.stop:
            return decision

    return CONTINUE


def rule_ids() -> tuple[str, ...]:
    """Every stop reason this module can produce. Used by the console legend."""
    return tuple(r.value for r in StopReason)
