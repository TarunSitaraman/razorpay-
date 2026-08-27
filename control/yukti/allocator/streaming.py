"""Real-time admission against an offline shadow price.

**The objection this answers.** The batch allocator solves one knapsack per
merchant per planning window. That is the right shape for an overdue invoice and
the wrong shape for an abandoned cart, where the value of acting decays over
minutes. A recovery system that can only decide on a cron schedule has already
lost the highest-intent surface it has.

**Why a second solver is not needed.** The Lagrangian solve produces more than
an allocation: it produces `lambda_contact` and `lambda_discount`, the marginal
value of one more unit of each budget. Those are prices, and a price is exactly
what turns a combinatorial batch decision into a local one. Given lambda, the
batch rule

    fund iff  margin - lambda_c . contacts - lambda_d . discount > 0

needs no knowledge of the other candidates at all. So the shadow price is
computed offline on yesterday's population, and the online path is a comparison
and a budget decrement — O(1) per event, no solver in the request path.

**What this costs, stated plainly.** An online policy has no hindsight. It
cannot decline a mediocre candidate at 09:00 because a better one will arrive at
16:00; it only knows the price. So it is strictly worse than the batch solve on
the same population, and `admission_gap` measures by how much rather than
asserting it is small. On the generated population that gap is a few percent of
margin, which is the honest trade for acting in milliseconds instead of hours.

**Where the price goes stale.** Lambda is a property of the population, not of
the candidate. If the mix shifts — a sale, an outage, a festival — yesterday's
price misvalues today's contacts, spending the budget too fast or too slow.
`utilisation` exposes the drift the operator needs to see, and the intended
cadence is a nightly recompute from the batch solve that already runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from yukti.allocator.lagrangian import Budgets, Candidate, allocate


class Verdict(StrEnum):
    """Why an offer was funded or refused.

    A named refusal rather than a bare `False`, for the same reason the planner
    has named stopping reasons: "we did not contact this customer" is a fact the
    merchant is owed an explanation for, and "the budget was gone" and "the
    price was too high" are different explanations with different fixes.
    """

    FUNDED = "funded"
    BELOW_PRICE = "below_price"            # did not clear its shadow cost
    BUDGET_EXHAUSTED = "budget_exhausted"  # merchant's authorised total is spent
    CUSTOMER_CAPPED = "customer_capped"    # this person's contact cap is used up
    CASE_ALREADY_FUNDED = "case_already_funded"


@dataclass(frozen=True, slots=True)
class Offer:
    verdict: Verdict
    reduced_value_paise: float

    @property
    def funded(self) -> bool:
        return self.verdict is Verdict.FUNDED


@dataclass(frozen=True, slots=True)
class ShadowPrice:
    """What one unit of each scarce budget is worth, in paise.

    Read off a batch solve rather than tuned. `lambda_contact` is directly
    quotable to a merchant: "another contact is worth this much to you", which is
    the number that tells them whether to raise the budget.
    """

    contact_paise: float
    discount_paise_per_paise: float
    # Population the price was fitted on, for staleness reporting.
    fitted_on_candidates: int = 0

    @classmethod
    def from_batch(cls, candidates: list[Candidate], budgets: Budgets) -> ShadowPrice:
        """Fit the price by solving one batch instance offline.

        This is the expensive call, and it is the one that never happens in the
        request path.
        """
        allocation = allocate(candidates, budgets)
        return cls(
            contact_paise=allocation.lambda_contact,
            discount_paise_per_paise=allocation.lambda_discount,
            fitted_on_candidates=len(candidates),
        )

    def reduced_value(self, c: Candidate) -> float:
        """Margin net of what the resources it consumes are worth elsewhere."""
        return (
            c.margin_paise
            - self.contact_paise * c.contacts
            - self.discount_paise_per_paise * c.discount_paise
        )


@dataclass(slots=True)
class StreamingAllocator:
    """Admission control for one merchant's budget window.

    Stateful by necessity — budgets deplete — so one instance owns one window.
    Not thread-safe: the intended deployment is one instance per partition
    consumer, which is also what keeps the per-customer cap correct, since a
    customer's events are keyed to a partition.
    """

    price: ShadowPrice
    budgets: Budgets
    contacts_used: int = 0
    discount_used_paise: int = 0
    funded_margin_paise: int = 0
    _cases: set[str] = field(default_factory=set)
    _per_customer: dict[str, int] = field(default_factory=dict)
    refusals: dict[Verdict, int] = field(default_factory=dict)

    def offer(self, c: Candidate) -> Offer:
        """Decide one candidate, now, without seeing any other.

        Order of the checks matters for the explanation, not the outcome: a
        candidate that both fails the price and has no budget left is reported as
        priced out, because raising the budget would not have funded it.
        """
        reduced = self.price.reduced_value(c)

        if c.case_id in self._cases:
            return self._refuse(Verdict.CASE_ALREADY_FUNDED, reduced)
        if reduced <= 0:
            return self._refuse(Verdict.BELOW_PRICE, reduced)
        if self.contacts_used + c.contacts > self.budgets.contacts:
            return self._refuse(Verdict.BUDGET_EXHAUSTED, reduced)
        if self.discount_used_paise + c.discount_paise > self.budgets.discount_paise:
            return self._refuse(Verdict.BUDGET_EXHAUSTED, reduced)
        if c.contacts:
            used = self._per_customer.get(c.customer_id, 0)
            if used + c.contacts > self.budgets.per_customer_contacts:
                return self._refuse(Verdict.CUSTOMER_CAPPED, reduced)

        self.contacts_used += c.contacts
        self.discount_used_paise += c.discount_paise
        self.funded_margin_paise += c.margin_paise
        self._cases.add(c.case_id)
        if c.contacts:
            self._per_customer[c.customer_id] = (
                self._per_customer.get(c.customer_id, 0) + c.contacts
            )
        return Offer(Verdict.FUNDED, reduced)

    def _refuse(self, verdict: Verdict, reduced: float) -> Offer:
        self.refusals[verdict] = self.refusals.get(verdict, 0) + 1
        return Offer(verdict, reduced)

    @property
    def utilisation(self) -> float:
        """Share of the contact budget spent. The staleness signal.

        Well above the elapsed share of the window means the price is too low for
        today's population and the budget will be gone by lunchtime; well below
        means it is too high and authorised money is going unspent. Either is a
        reason to refit.
        """
        if self.budgets.contacts <= 0:
            return 0.0
        return self.contacts_used / self.budgets.contacts


def admission_gap(candidates: list[Candidate], budgets: Budgets) -> tuple[int, int, float]:
    """How much margin the online policy gives up against the batch solve.

    Returns `(batch, streamed, ratio)` in paise. The candidates are streamed in
    the order given, which is the point: a different arrival order produces a
    different result, and reporting the ratio for a realistic order is the only
    honest way to characterise an online policy.

    Used by the tests to hold the regret to a stated bound rather than to a hope.
    """
    batch = allocate(candidates, budgets)
    price = ShadowPrice.from_batch(candidates, budgets)
    streamer = StreamingAllocator(price=price, budgets=budgets)
    for c in candidates:
        streamer.offer(c)

    batch_margin = batch.total_margin_paise
    ratio = streamer.funded_margin_paise / batch_margin if batch_margin > 0 else 1.0
    return batch_margin, streamer.funded_margin_paise, ratio
