"""The service-free bundle has to be shaped like an evaluation, and honest.

`make demo-light` is the path for a judge who cannot run Kafka, Postgres and
Redis: the console serves `artifacts/demo-results.json` from `/metrics/lift`
when no real evaluation exists. Two failures are worth pinning.

The first is structural — the bundle used to be a convenient dump whose keys the
console did not read, so the one panel a service-free run can populate rendered
empty.

The second is a lie. The interval was written as `[point, high]` and labelled a
95% CI, which turns an interval comfortably containing zero into one that
appears to exclude it — the exact overstatement this project spends its
evaluation section arguing against.
"""

from __future__ import annotations

from yukti.eval.demo_bundle import ARM_KEYS, build

# One axis, two grid points, in the shape `sensitivity.json` uses.
FIXTURE = {
    "persuadable_uplift": {
        "description": "headroom on the only profitable archetype",
        "crossover": 0.097,
        "points": [
            {"value": 0.46, "winner": "Y", "arms": {
                k: {"contacts": 200, "recovered_cases": 700 + i,
                    "opt_outs": 1, "incremental_paise": 80_000_000 + i,
                    "contact_attributable_paise": 2_000_000 + i,
                    "per_1k_point": 700_000.0, "per_1k_lo": -600_000.0,
                    "per_1k_hi": 2_200_000.0,
                    "contacted_by_archetype": {"persuadable": 66}}
                for i, k in enumerate(ARM_KEYS)}},
            {"value": 0.06, "winner": "B1", "arms": {
                k: {"contacts": 200, "recovered_cases": 400,
                    "opt_outs": 1, "incremental_paise": 15_000_000,
                    "contact_attributable_paise": -50_000,
                    "per_1k_point": -10_000.0, "per_1k_lo": -900_000.0,
                    "per_1k_hi": 800_000.0,
                    "contacted_by_archetype": {"persuadable": 3}}
                for k in ARM_KEYS}},
        ],
    },
}

# What `app.js` actually reads off an arm.
CONSOLE_ARM_FIELDS = (
    "key", "label", "acts", "contacts", "recovered_cases",
    "net_incremental_paise", "contact_incremental_paise", "contact_per_1k",
)


def test_the_bundle_carries_every_field_the_console_reads():
    bundle = build(FIXTURE)

    assert {a["key"] for a in bundle["arms"]} == set(ARM_KEYS)
    for arm in bundle["arms"]:
        for field in CONSOLE_ARM_FIELDS:
            assert field in arm, f"{arm['key']} is missing {field}"
        assert set(arm["contact_per_1k"]) == {"point", "low", "high", "excludes_zero"}


def test_the_interval_is_the_interval_not_the_point_estimate():
    """The bug this file exists for: `[point, high]` sold as a 95% CI."""
    arm = next(a for a in build(FIXTURE)["arms"] if a["key"] == "Y")

    assert arm["contact_per_1k"]["low"] == -600_000.0
    assert arm["contact_per_1k"]["high"] == 2_200_000.0
    assert arm["contact_per_1k"]["point"] == 700_000.0


def test_an_interval_containing_zero_does_not_claim_to_exclude_it():
    arm = next(a for a in build(FIXTURE)["arms"] if a["key"] == "Y")

    assert arm["contact_per_1k"]["excludes_zero"] is False


def test_the_bundle_says_which_world_it_came_from():
    """Otherwise the console presents a simulation as a merchant's book."""
    bundle = build(FIXTURE)

    assert bundle["source"] == "demo-light"
    assert bundle["merchant_id"] is None


def test_the_arm_that_never_contacts_anyone_is_marked_as_not_acting():
    """`retry-only` is the reference the others are measured against; charting
    it as a competing strategy is what made an earlier console double-count the
    free retries every arm shares."""
    arms = {a["key"]: a for a in build(FIXTURE)["arms"]}

    assert arms["B4"]["acts"] is False
    assert all(arms[k]["acts"] for k in ARM_KEYS if k != "B4")
