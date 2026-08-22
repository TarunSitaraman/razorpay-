"""Archetype must be inferable from observable history — and only from it.

This file records two corrections, because the reasoning is the interesting
part.

**First defect.** Archetype was drawn independently of every observable, so
conditional on features the expected uplift was identical across archetypes and
no model could separate a sleeping dog from a sure thing. A learner would still
have picked up the situational half of uplift and plausibly cleared a naive
AUUC bar — passing on the easy half while the headline claim is provably
unlearnable, with nothing looking wrong.

**Second defect.** The first fix generated a history and then drew archetype
conditioned on it. Better, but it left sure things and persuadables with nearly
identical observable profiles (tenure 432 vs 345, prior payments 5.7 vs 3.4)
despite control recovery rates of 0.707 vs 0.089. Measured ceiling from
observables was AUC 0.62 against 0.861 using the archetype directly, and no
hyperparameter setting moved it: the information was not in the features.

The fix was to invert the causal direction. Archetype is the latent cause of
the payment record, not a consequence of it, and the discriminator is whether
past payments followed a nudge — which a real system genuinely observes.
Observables now reach AUC 0.864 against a 0.876 ceiling.
"""

from __future__ import annotations

import math
import random
from collections import Counter

import pytest
from yukti.domain.enums import UpliftArchetype
from yukti_datagen.world import (
    ARCHETYPE_BEHAVIOUR,
    ARCHETYPE_MIX,
    MERCHANTS,
    build_history,
    build_world,
)

SEED = 20260822


@pytest.fixture(scope="module")
def customers():
    world = build_world(random.Random(SEED), 2000)
    return [c for m in world for c in m.customers]


def mutual_information(pairs: list[tuple[str, str]]) -> float:
    """I(X;Y) in bits. Zero means X carries no information about Y."""
    n = len(pairs)
    joint, mx, my = Counter(pairs), Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    return sum(
        (c / n) * math.log2((c / n) / ((mx[a] / n) * (my[b] / n)))
        for (a, b), c in joint.items()
    )


class TestArchetypeIsLearnable:
    def test_history_carries_information_about_archetype(self, customers):
        pairs = [(c.history.profile, c.archetype.value) for c in customers]
        # Was exactly 0.0 under the first design.
        assert mutual_information(pairs) > 0.30

    def test_prompted_share_separates_sure_things_from_persuadables(self, customers):
        """The discriminator the second fix added.

        These two archetypes are the pair the whole thesis depends on, and under
        the previous design they were observationally identical.
        """
        def share(archetype: UpliftArchetype) -> float:
            sub = [c for c in customers if c.archetype is archetype]
            return sum(
                c.history.prior_prompted_payments
                / max(1, c.history.prior_payments)
                for c in sub
            ) / len(sub)

        # A persuadable's payments mostly followed a nudge; a sure thing's did not.
        assert share(UpliftArchetype.PERSUADABLE) > 3 * share(UpliftArchetype.SURE_THING)

    def test_unprompted_payments_separate_sure_things_from_lost_causes(self, customers):
        def mean_unprompted(archetype: UpliftArchetype) -> float:
            sub = [c for c in customers if c.archetype is archetype]
            return sum(c.history.prior_unprompted_payments for c in sub) / len(sub)

        assert mean_unprompted(UpliftArchetype.SURE_THING) > 3 * mean_unprompted(
            UpliftArchetype.LOST_CAUSE
        )

    def test_sleeping_dogs_have_elevated_optout_history(self, customers):
        def optout_rate(archetype: UpliftArchetype) -> float:
            sub = [c for c in customers if c.archetype is archetype]
            return sum(c.history.prior_optouts for c in sub) / len(sub)

        # What distinguishes a sleeping dog from a sure thing: both pay when
        # left alone, but one has already told us to stop contacting them.
        assert optout_rate(UpliftArchetype.SLEEPING_DOG) > 3 * optout_rate(
            UpliftArchetype.SURE_THING
        )

    def test_archetype_is_not_readable_for_most_customers(self, customers):
        """Inferable, not readable — for any material share of the population.

        A deterministic mapping would make the problem trivial and would be
        leakage wearing a different name.

        The threshold is deliberately mass-weighted rather than applied to every
        bucket. Extreme tails ARE separable and should be: someone with eight
        nudge-driven payments and no unprompted ones really is a persuadable,
        and any generative model with different means will have separable tails.
        What would be dishonest is the archetype being readable for a large
        fraction of customers, so the test covers buckets holding at least 2% of
        the population and leaves the thin tail alone.
        """
        buckets: dict[int, Counter] = {}
        for c in customers:
            buckets.setdefault(c.history.prior_prompted_payments, Counter())[
                c.archetype.value
            ] += 1

        material = max(50, int(0.02 * len(customers)))
        checked = 0
        for value, counts in buckets.items():
            total = sum(counts.values())
            if total < material:
                continue
            checked += 1
            assert max(counts.values()) / total < 0.95, (
                f"prompted_payments={value} determines the archetype "
                f"for {total} customers"
            )
        assert checked >= 3, "too few material buckets to make this test meaningful"

    def test_the_decision_boundary_is_genuinely_uncertain(self, customers):
        """Around the modal separating value, the archetype must be ambiguous.

        This is where the model actually has to work. If the boundary were
        clean, the gate would be measuring a lookup rather than an inference.
        """
        boundary = Counter(
            c.archetype.value for c in customers
            if c.history.prior_prompted_payments in (3, 4)
        )
        total = sum(boundary.values())
        assert total > 0
        assert max(boundary.values()) / total < 0.75


class TestMarginalMixPreserved:
    @pytest.mark.parametrize("archetype", list(UpliftArchetype))
    def test_mix_stays_on_target(self, customers, archetype):
        actual = sum(c.archetype is archetype for c in customers) / len(customers)
        # Exact by construction now: archetype is drawn first. The tolerance
        # covers only sampling noise and the documented B2B override.
        assert abs(actual - ARCHETYPE_MIX[archetype]) < 0.035

    def test_every_archetype_is_represented(self, customers):
        assert {c.archetype for c in customers} == set(UpliftArchetype)


class TestBehaviourTable:
    @pytest.mark.parametrize("archetype", list(UpliftArchetype))
    def test_every_archetype_has_a_behaviour_profile(self, archetype):
        assert archetype in ARCHETYPE_BEHAVIOUR

    @pytest.mark.parametrize("archetype", list(UpliftArchetype))
    def test_profiles_declare_every_trait(self, archetype):
        required = {
            "tenure", "unprompted_payments", "prompted_payments", "failures",
            "contacts", "days_since_payment", "optout_rate",
        }
        assert required <= set(ARCHETYPE_BEHAVIOUR[archetype])

    def test_persuadables_are_the_most_nudge_responsive(self):
        prompted = {
            a: ARCHETYPE_BEHAVIOUR[a]["prompted_payments"][0] for a in UpliftArchetype
        }
        assert max(prompted, key=prompted.get) is UpliftArchetype.PERSUADABLE

    def test_sleeping_dogs_opt_out_most(self):
        rates = {a: ARCHETYPE_BEHAVIOUR[a]["optout_rate"] for a in UpliftArchetype}
        assert max(rates, key=rates.get) is UpliftArchetype.SLEEPING_DOG


class TestHistoryIsSelfConsistent:
    def test_fields_are_non_negative_and_coherent(self):
        rng = random.Random(3)
        for archetype in UpliftArchetype:
            for _ in range(200):
                h = build_history(rng, MERCHANTS[0], archetype)
                assert h.tenure_days >= 1
                assert h.prior_payments == (
                    h.prior_unprompted_payments + h.prior_prompted_payments
                )
                assert h.prior_contacts >= h.prior_prompted_payments
                assert 0 <= h.prior_contact_responses <= h.prior_contacts

    def test_customers_who_never_paid_look_dormant(self):
        rng = random.Random(5)
        for archetype in UpliftArchetype:
            for _ in range(300):
                h = build_history(rng, MERCHANTS[0], archetype)
                if h.prior_payments == 0:
                    # The generator must not produce someone who never paid but
                    # paid last week.
                    assert h.days_since_last_payment >= 90
