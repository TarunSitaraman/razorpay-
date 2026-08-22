"""The exploration history must be a valid randomised trial.

Uplift is only identified when treatment assignment is independent of the
outcome. If the exploration policy peeked at the customer — even indirectly,
via amount or history — the resulting model would relearn the policy rather
than the causal effect, and every lift number downstream would be wrong in a
way no amount of held-out data would reveal.

So independence is asserted, not assumed.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from datetime import datetime

import pytest
from yukti.domain.enums import ActionKind, Channel, UpliftArchetype
from yukti_datagen.history import (
    CHANNEL_COST_PAISE,
    EXPLORE_ACTIONS,
    HistoricalTreatment,
    run_exploration,
    sample_intervention,
)

AT = datetime(2026, 5, 12, 10, 0)


class TestAssignmentIsBlind:
    def test_sample_intervention_reads_nothing_about_the_customer(self):
        # The signature is the guarantee: there is no customer argument to peek
        # at. A reviewer can verify independence by reading one line.
        import inspect

        params = set(inspect.signature(sample_intervention).parameters)
        assert params == {"rng", "at"}

    def test_two_identical_seeds_give_identical_draws(self):
        a = sample_intervention(random.Random(1), AT)
        b = sample_intervention(random.Random(1), AT)
        assert (a.kind, a.channel, a.at, a.discount_pct) == (b.kind, b.channel, b.at, b.discount_pct)

    def test_action_mix_matches_the_declared_policy(self):
        rng = random.Random(99)
        draws = Counter(sample_intervention(rng, AT).kind for _ in range(20_000))
        total = sum(draws.values())
        for kind, want in EXPLORE_ACTIONS.items():
            assert abs(draws[kind] / total - want) < 0.02, f"{kind} off-policy"

    def test_hours_span_the_whole_day(self):
        # Exploration must sample outside TRAI hours. If it never did, the model
        # could not learn what the 09:00-21:00 restriction costs, and the
        # constraint would look free to the allocator.
        rng = random.Random(4)
        hours = {sample_intervention(rng, AT).at.hour for _ in range(2000)}
        assert min(hours) < 9 and max(hours) > 21

    def test_discounts_only_attach_to_discount_offers(self):
        rng = random.Random(8)
        for _ in range(3000):
            iv = sample_intervention(rng, AT)
            if iv.kind is not ActionKind.DISCOUNT_OFFER:
                assert iv.discount_pct == 0.0

    def test_non_contact_actions_carry_no_channel(self):
        rng = random.Random(12)
        for _ in range(3000):
            iv = sample_intervention(rng, AT)
            if iv.kind in (ActionKind.SUPPRESS, ActionKind.SILENT_RETRY,
                           ActionKind.SCHEDULE_DEBIT):
                assert iv.channel is Channel.NONE


def _synthetic_cases(n: int) -> tuple[list[dict], dict[str, dict]]:
    rng = random.Random(3)
    cases, customers = [], {}
    archetypes = list(UpliftArchetype)
    for i in range(n):
        cid = f"cus_{i}"
        customers[cid] = {
            "archetype": archetypes[i % len(archetypes)].value,
            "preferred_channel": "whatsapp",
            "issuer": "HDFC",
        }
        cases.append({
            "case_id": f"case_{i}", "obligation_id": f"obl_{i}",
            "merchant_id": "mrc_1", "customer_id": cid,
            "amount_paise": rng.randint(50_000, 500_000),
            "decline_code": "INSUFFICIENT_FUNDS", "rail_is_mandate": True,
            "ts": AT,
        })
    return cases, customers


class TestBalance:
    @pytest.fixture(scope="class")
    def treatments(self) -> list[HistoricalTreatment]:
        cases, customers = _synthetic_cases(4000)
        return run_exploration(cases, customers, downtime=[], seed=20260822)

    def test_archetype_mix_is_balanced_across_arms(self, treatments):
        def mix(rows):
            c = Counter(t.archetype for t in rows)
            n = sum(c.values())
            return {k: v / n for k, v in c.items()}

        control = mix([t for t in treatments if t.action_kind == "suppress"])
        treated = mix([t for t in treatments if t.action_kind != "suppress"])
        # Imbalance here means assignment leaked the archetype.
        for arch in control:
            assert abs(control[arch] - treated.get(arch, 0)) < 0.05

    def test_assignment_carries_no_information_about_archetype(self, treatments):
        pairs = [
            ("control" if t.action_kind == "suppress" else "treated", t.archetype)
            for t in treatments
        ]
        n = len(pairs)
        joint, mx, my = Counter(pairs), Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
        mi = sum(
            (c / n) * math.log2((c / n) / ((mx[a] / n) * (my[b] / n)))
            for (a, b), c in joint.items()
        )
        # Should be indistinguishable from zero. Contrast with
        # I(history; archetype) ~= 0.33 bits, which is signal we WANT.
        assert mi < 0.01, f"treatment assignment leaks archetype: {mi:.4f} bits"

    def test_control_group_is_substantial(self, treatments):
        control = sum(1 for t in treatments if t.action_kind == "suppress")
        assert 0.15 < control / len(treatments) < 0.35


class TestUpliftSignalIsPresent:
    """The observed data must show the divergence, not just the oracle."""

    @pytest.fixture(scope="class")
    def rates(self):
        cases, customers = _synthetic_cases(8000)
        rows = run_exploration(cases, customers, downtime=[], seed=7)

        def rate(arch: str, treated: bool) -> float:
            sub = [
                t for t in rows
                if t.archetype == arch and (t.action_kind != "suppress") == treated
            ]
            return sum(t.recovered for t in sub) / len(sub) if sub else 0.0

        return {a.value: (rate(a.value, False), rate(a.value, True)) for a in UpliftArchetype}

    def test_persuadables_show_large_positive_uplift(self, rates):
        control, treated = rates["persuadable"]
        assert treated - control > 0.08

    def test_sleeping_dogs_show_negative_uplift(self, rates):
        control, treated = rates["sleeping_dog"]
        assert treated < control

    def test_lost_causes_show_near_zero_uplift(self, rates):
        control, treated = rates["lost_cause"]
        assert abs(treated - control) < 0.05

    def test_propensity_and_uplift_disagree_in_the_observed_data(self, rates):
        # The whole thesis, visible in the RCT rather than assumed from the
        # oracle: highest treated recovery rate is NOT highest causal effect.
        by_propensity = max(rates, key=lambda a: rates[a][1])
        by_uplift = max(rates, key=lambda a: rates[a][1] - rates[a][0])
        assert by_propensity == "sure_thing"
        assert by_uplift == "persuadable"


class TestCosts:
    def test_every_channel_has_a_cost(self):
        assert set(CHANNEL_COST_PAISE) == set(Channel)

    def test_voice_is_the_most_expensive_channel(self):
        # The allocator optimises net margin, so a voice call that costs more
        # than it recovers has to be visibly unprofitable in training data.
        assert CHANNEL_COST_PAISE[Channel.VOICE] == max(CHANNEL_COST_PAISE.values())

    def test_suppression_is_free(self):
        cases, customers = _synthetic_cases(500)
        rows = run_exploration(cases, customers, downtime=[], seed=2)
        for t in rows:
            if t.action_kind == "suppress":
                assert t.cost_paise == 0 and t.discount_paise == 0
