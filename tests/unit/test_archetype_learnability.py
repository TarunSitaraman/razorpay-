"""Archetype must be inferable from observable history — and only from it.

Regression test for a defect that would have made the project's central claim
unlearnable while every metric still looked healthy.

Archetype was drawn independently of every observable feature. Conditional on
features, expected uplift was therefore identical across archetypes, so no
model could separate a sleeping dog from a sure thing. A learner would still
have picked up the *situational* half of uplift — suppress during downtime,
skip permanent declines, respect fatigue — and plausibly cleared a naive AUUC
gate. Passing on the easy half while the headline claim is provably
unlearnable is worse than failing outright, because nothing looks wrong.

Two properties are required together, and they pull against each other:
  1. archetype must depend on observables (or it cannot be learned);
  2. the marginal mix must stay on target (or conditioning has quietly changed
     how many sleeping dogs exist in the world, not just which customers they
     are).
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict

import pytest

from yukti_datagen.world import (
    ARCHETYPE_GIVEN_HISTORY,
    ARCHETYPE_MIX,
    MERCHANTS,
    build_history,
    build_world,
)

from yukti.domain.enums import UpliftArchetype

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
        # Was exactly 0.0 before the fix. Anything near zero means unlearnable.
        assert mutual_information(pairs) > 0.15

    def test_tenure_alone_carries_signal(self, customers):
        pairs = [
            ("long" if c.tenure_days > 400 else "short", c.archetype.value)
            for c in customers
        ]
        assert mutual_information(pairs) > 0.005

    def test_prior_optout_strongly_predicts_sleeping_dog(self, customers):
        def share(flag: int) -> float:
            sub = [c for c in customers if c.history.prior_optouts == flag]
            if not sub:
                return 0.0
            return sum(c.archetype is UpliftArchetype.SLEEPING_DOG for c in sub) / len(sub)

        # Someone who has opted out before resents being chased. That is the
        # single most actionable observable in the whole dataset.
        assert share(1) > 3 * share(0)

    def test_long_tenure_predicts_sure_thing(self, customers):
        def share(long: bool) -> float:
            sub = [c for c in customers if (c.tenure_days > 500) == long]
            return sum(c.archetype is UpliftArchetype.SURE_THING for c in sub) / len(sub)

        assert share(True) > share(False)


class TestMarginalMixPreserved:
    @pytest.mark.parametrize("archetype", list(UpliftArchetype))
    def test_mix_stays_on_target(self, customers, archetype):
        actual = sum(c.archetype is archetype for c in customers) / len(customers)
        # Tolerance accommodates the documented B2B override, which converts
        # sleeping dogs to persuadables for institutional buyers.
        assert abs(actual - ARCHETYPE_MIX[archetype]) < 0.035

    def test_every_archetype_is_represented(self, customers):
        assert {c.archetype for c in customers} == set(UpliftArchetype)


class TestConditioningTable:
    def test_every_profile_has_a_row(self):
        rng = random.Random(11)
        seen = {build_history(rng, MERCHANTS[0]).profile for _ in range(5000)}
        assert seen <= set(ARCHETYPE_GIVEN_HISTORY), "a profile has no conditioning row"

    @pytest.mark.parametrize("profile", sorted(ARCHETYPE_GIVEN_HISTORY))
    def test_rows_cover_all_archetypes_with_positive_weight(self, profile):
        row = ARCHETYPE_GIVEN_HISTORY[profile]
        assert set(row) == set(UpliftArchetype)
        # No archetype may be impossible given a history: that would make the
        # profile a deterministic readout of the label and leak it as a feature.
        assert all(w > 0 for w in row.values())

    def test_each_profile_favours_its_intended_archetype(self):
        intended = {
            "loyal_clean": UpliftArchetype.SURE_THING,
            "engaged_responsive": UpliftArchetype.PERSUADABLE,
            "dormant_failing": UpliftArchetype.LOST_CAUSE,
            "annoyed_withdrawn": UpliftArchetype.SLEEPING_DOG,
        }
        for profile, expected in intended.items():
            row = ARCHETYPE_GIVEN_HISTORY[profile]
            assert max(row, key=row.get) is expected, f"{profile} should favour {expected}"


class TestHistoryIsSelfConsistent:
    def test_fields_are_non_negative(self):
        rng = random.Random(3)
        for _ in range(500):
            h = build_history(rng, MERCHANTS[0])
            assert h.tenure_days >= 1
            assert h.prior_payments >= 0 and h.prior_failures >= 0
            assert 0 <= h.prior_contact_responses <= h.prior_contacts

    def test_customers_with_no_payments_look_dormant(self):
        rng = random.Random(5)
        samples = [build_history(rng, MERCHANTS[0]) for _ in range(2000)]
        never_paid = [h for h in samples if h.prior_payments == 0]
        assert never_paid, "expected some customers to have never paid"
        # Not having paid recently is what "dormant" means; the generator must
        # not produce a customer who never paid but paid last week.
        assert all(h.days_since_last_payment >= 60 for h in never_paid)
