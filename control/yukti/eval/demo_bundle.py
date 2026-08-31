"""Build `artifacts/demo-results.json` — the service-free stand-in for `make eval`.

`make demo-light` exists so a judge who cannot run Kafka, Postgres and Redis can
still see the measured result. The console reads it from `/metrics/lift` when no
real evaluation is on disk, which means this file has to be shaped like an
evaluation report rather than like a convenient dump — the previous version was
neither, and the console rendered nothing from it.

Two things this script is careful about:

* **The interval is the interval.** The earlier version wrote
  `[per_1k_point, per_1k_hi]` and called it a 95% CI, which turned an interval
  that comfortably contains zero into one that appears to exclude it. In a
  project whose entire argument is that honest measurement is expensive, that is
  the worst possible bug to ship.
* **It says which world it is.** `source: "demo-light"` travels with the
  numbers, so the console can label them as a service-free simulation rather
  than as a merchant's book.
"""

from __future__ import annotations

import json
import math
import pathlib

SENSITIVITY = pathlib.Path("artifacts/sensitivity.json")
OUT = pathlib.Path("artifacts/demo-results.json")

# The axis the headline sits on, and the two ends of it worth contrasting.
HEADLINE_AXIS = "persuadable_uplift"

ARM_KEYS = ("B4", "B1", "B3", "Y")
ARM_LABELS = {
    "B4": "retry-only",
    "B1": "fixed cadence",
    "B3": "propensity only",
    "Y": "Niyama (uplift)",
}
# B4 never contacts anyone: it is the reference every other arm is measured
# against, not a strategy competing with them.
ACTS = {"B4": False, "B1": True, "B3": True, "Y": True}


def _interval(arm: dict) -> dict:
    low, high = arm["per_1k_lo"], arm["per_1k_hi"]
    return {
        "point": arm["per_1k_point"],
        "low": low,
        "high": high,
        "excludes_zero": (low > 0) or (high < 0),
    }


def build(sens: dict) -> dict:
    points = sens[HEADLINE_AXIS]["points"]
    rich, poor = points[0], points[-1]

    arms = []
    for key in ARM_KEYS:
        a = rich["arms"][key]
        arms.append({
            "key": key,
            "label": ARM_LABELS[key],
            "acts": ACTS[key],
            "contacts": a["contacts"],
            "recovered_cases": a["recovered_cases"],
            "opt_outs": a.get("opt_outs", 0),
            # Measured against the holdout world, as the sweep reports it.
            "net_incremental_paise": a["incremental_paise"],
            # Measured against retry-only, which isolates the contact decision.
            "contact_incremental_paise": a["contact_attributable_paise"],
            "contact_per_1k": _interval(a),
            "contacted_by_archetype": a.get("contacted_by_archetype", {}),
        })

    # The power disclosure, from EVALUATION.md §3. Restated rather than
    # recomputed: it is a property of the merchant book the headline was
    # measured on, not of this simulation.
    per_case_sd, effect_size, holdout_fraction = 12_870, 315, 0.10
    factor = 1 / (1 - holdout_fraction) + 1 / holdout_fraction
    cases_needed = math.ceil(factor * (2.8 * per_case_sd / effect_size) ** 2)

    return {
        "source": "demo-light",
        "world": (
            "service-free simulation — the sensitivity harness's default world, "
            "generated, learned and graded in process"
        ),
        "axis": HEADLINE_AXIS,
        "merchant_id": None,
        "as_of": None,
        "cases": None,
        "arms": arms,
        "frontier": {
            axis: {
                "description": data["description"],
                "crossover": data["crossover"],
            }
            for axis, data in sens.items()
        },
        "contrast": {
            "axis": HEADLINE_AXIS,
            "rich_value": rich["value"],
            "poor_value": poor["value"],
            "by_arm": {
                key: {
                    "rich_incremental_paise": rich["arms"][key]["incremental_paise"],
                    "poor_incremental_paise": poor["arms"][key]["incremental_paise"],
                }
                for key in ARM_KEYS
            },
        },
        "power": {
            "per_case_sd_paise": per_case_sd,
            "effect_size_paise": effect_size,
            "cases_needed_for_80pct_power": cases_needed,
            "available_cases": 3_475,
            "shortfall_vs_80pct": "39×",
        },
    }


def main() -> None:
    sens = json.loads(SENSITIVITY.read_text(encoding="utf-8"))
    OUT.write_text(json.dumps(build(sens), indent=2), encoding="utf-8")
    print(f"  ok   generated {OUT}")


if __name__ == "__main__":
    main()
