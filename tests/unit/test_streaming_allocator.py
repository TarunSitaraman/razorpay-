"""Real-time admission: the properties that make it safe to put in the path.

An online allocator is easy to get subtly wrong in the expensive direction — it
holds the budget, and a bookkeeping slip overspends real money rather than
crashing. So the tests here are mostly about what it must NEVER do, with one
test for how much margin the online policy gives up against the batch solve.

That regret test is bounded rather than asserted-to-be-small: an online policy
has no hindsight and is strictly worse than batch, so the useful question is by
how much, and whether that number moves when someone changes the pricing rule.
"""

from __future__ import annotations

import random

import pytest
from yukti.allocator.lagrangian import Budgets, Candidate, allocate
from yukti.allocator.streaming import (
    ShadowPrice,
    StreamingAllocator,
    Verdict,
    admission_gap,
)


def _population(n: int, seed: int) -> list[Candidate]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        contacts = rng.choice([0, 1, 1])
        out.append(
            Candidate(
                case_id=f"case{i}",
                customer_id=f"cust{rng.randrange(max(1, n // 3))}",
                action_kind="message" if contacts else "silent_retry",
                channel="sms" if contacts else "none",
                margin_paise=rng.randint(-5_000, 90_000),
                contacts=contacts,
                discount_paise=rng.choice([0, 0, 5_000, 20_000]),
                channel_cost_paise=25 if contacts else 0,
            )
        )
    return out


def _budgets(contacts: int = 60, discount: int = 400_000) -> Budgets:
    return Budgets(contacts=contacts, discount_paise=discount, per_customer_contacts=1)


def _streamer(candidates: list[Candidate], budgets: Budgets) -> StreamingAllocator:
    return StreamingAllocator(
        price=ShadowPrice.from_batch(candidates, budgets), budgets=budgets
    )


class TestBudgetsAreNeverExceeded:
    """The merchant authorised a number. Online or not, it is a hard limit."""

    @pytest.mark.parametrize("seed", range(25))
    def test_contact_budget_holds_under_any_arrival_order(self, seed: int) -> None:
        candidates, budgets = _population(1_200, seed), _budgets()
        rng = random.Random(seed)
        shuffled = candidates[:]
        rng.shuffle(shuffled)

        s = _streamer(candidates, budgets)
        for c in shuffled:
            s.offer(c)
        assert s.contacts_used <= budgets.contacts

    @pytest.mark.parametrize("seed", range(25))
    def test_discount_budget_holds(self, seed: int) -> None:
        candidates, budgets = _population(1_200, seed), _budgets()
        s = _streamer(candidates, budgets)
        for c in candidates:
            s.offer(c)
        assert s.discount_used_paise <= budgets.discount_paise

    def test_a_zero_budget_funds_no_contact(self) -> None:
        candidates = _population(500, 1)
        budgets = Budgets(contacts=0, discount_paise=0, per_customer_contacts=0)
        s = _streamer(candidates, budgets)
        funded = [c for c in candidates if s.offer(c).funded]
        assert all(c.contacts == 0 and c.discount_paise == 0 for c in funded)


class TestPerCustomerCap:
    """The cap is on the person, across surfaces. It is the whole product."""

    def test_one_customer_cannot_be_contacted_twice(self) -> None:
        budgets = Budgets(contacts=50, discount_paise=0, per_customer_contacts=1)
        candidates = [
            Candidate(f"case{i}", "same_person", "message", "sms", 50_000, 1, 0, 25)
            for i in range(8)
        ]
        s = StreamingAllocator(price=ShadowPrice(0.0, 0.0), budgets=budgets)
        verdicts = [s.offer(c).verdict for c in candidates]

        assert verdicts[0] is Verdict.FUNDED
        assert all(v is Verdict.CUSTOMER_CAPPED for v in verdicts[1:])
        assert s.contacts_used == 1

    def test_the_same_case_is_never_funded_twice(self) -> None:
        """Two proposals for one obligation would chase a single debt twice."""
        budgets = Budgets(contacts=50, discount_paise=0, per_customer_contacts=5)
        a = Candidate("case1", "cust1", "message", "sms", 50_000, 1, 0, 25)
        b = Candidate("case1", "cust1", "message", "whatsapp", 40_000, 1, 0, 75)
        s = StreamingAllocator(price=ShadowPrice(0.0, 0.0), budgets=budgets)

        assert s.offer(a).funded
        assert s.offer(b).verdict is Verdict.CASE_ALREADY_FUNDED
        assert s.contacts_used == 1


class TestPricing:
    def test_a_candidate_below_the_price_is_refused(self) -> None:
        budgets = _budgets()
        cheap = Candidate("case1", "cust1", "message", "sms", 100, 1, 0, 25)
        s = StreamingAllocator(price=ShadowPrice(50_000.0, 0.0), budgets=budgets)

        offer = s.offer(cheap)
        assert offer.verdict is Verdict.BELOW_PRICE
        assert offer.reduced_value_paise < 0
        assert s.contacts_used == 0

    def test_a_free_action_is_funded_whatever_the_contact_price(self) -> None:
        """A silent retry consumes no priced resource, so no price can exclude it."""
        budgets = _budgets()
        free = Candidate("case1", "cust1", "silent_retry", "none", 5_000, 0, 0, 0)
        s = StreamingAllocator(price=ShadowPrice(9_999_999.0, 0.0), budgets=budgets)
        assert s.offer(free).funded

    def test_price_is_read_off_the_batch_solve(self) -> None:
        candidates, budgets = _population(2_000, 7), _budgets()
        price = ShadowPrice.from_batch(candidates, budgets)
        allocation = allocate(candidates, budgets)

        assert price.contact_paise == allocation.lambda_contact
        assert price.discount_paise_per_paise == allocation.lambda_discount
        assert price.fitted_on_candidates == len(candidates)


class TestRegretAgainstBatch:
    """How much the absence of hindsight costs, measured rather than assumed."""

    @pytest.mark.parametrize("seed", range(10))
    def test_online_never_beats_batch_by_much_and_stays_close(self, seed: int) -> None:
        candidates, budgets = _population(3_000, seed), _budgets()
        batch, streamed, ratio = admission_gap(candidates, budgets)

        assert batch > 0
        # Bounded regret. Measured at ~0.1% on this population; the bound is set
        # well outside that so it fails on a real regression rather than on
        # noise from a distribution change.
        assert ratio >= 0.95, f"streaming gave up {(1 - ratio) * 100:.1f}% of margin"
        # It may match batch, but it cannot exceed it: batch chooses with full
        # knowledge of the population from the same candidate set.
        assert streamed <= batch + 1


class TestOperability:
    def test_utilisation_tracks_the_contact_budget(self) -> None:
        budgets = Budgets(contacts=10, discount_paise=0, per_customer_contacts=1)
        s = StreamingAllocator(price=ShadowPrice(0.0, 0.0), budgets=budgets)
        assert s.utilisation == 0.0

        for i in range(5):
            s.offer(Candidate(f"case{i}", f"cust{i}", "message", "sms", 50_000, 1, 0, 25))
        assert s.utilisation == pytest.approx(0.5)

    def test_utilisation_is_defined_when_no_contacts_are_authorised(self) -> None:
        """A zero budget must report 0.0, not divide by zero in the dashboard."""
        s = StreamingAllocator(
            price=ShadowPrice(0.0, 0.0),
            budgets=Budgets(contacts=0, discount_paise=0, per_customer_contacts=0),
        )
        assert s.utilisation == 0.0

    def test_every_refusal_is_counted_under_a_named_reason(self) -> None:
        """Unexplained silence is the failure mode the stopping rules exist to
        prevent; the online path must not reintroduce it."""
        budgets = Budgets(contacts=1, discount_paise=0, per_customer_contacts=1)
        s = StreamingAllocator(price=ShadowPrice(0.0, 0.0), budgets=budgets)
        candidates = [
            Candidate(f"case{i}", f"cust{i}", "message", "sms", 50_000, 1, 0, 25)
            for i in range(6)
        ]
        offers = [s.offer(c) for c in candidates]

        refused = [o for o in offers if not o.funded]
        assert len(refused) == 5
        assert sum(s.refusals.values()) == len(refused)
        assert all(o.verdict is not Verdict.FUNDED for o in refused)
