#!/usr/bin/env python3
"""Generate artifacts/demo-results.json from existing sensitivity data."""
import json
import pathlib
import math

with open("artifacts/sensitivity.json") as f:
    sens = json.load(f)

report = {"arm_results": {}, "comparison": {}, "power": {}}

pau_data = sens["persuadable_uplift"]
points = pau_data["points"]

ARM_KEYS = ("B4", "B1", "B3", "Y")

for arm in ARM_KEYS:
    rich = points[0]  # value=0.46
    poor = points[-1]  # value=0.06
    a = rich["arms"][arm]
    b = poor["arms"][arm]
    report["arm_results"][arm] = {
        "contacts": a["contacts"],
        "recovered_cases": a["recovered_cases"],
        "incremental_paise": a["incremental_paise"],
        "contact_attributable_paise": a["contact_attributable_paise"],
        "per_1k": (
            f"[{a['per_1k_point']:.0f}, {a['per_1k_hi']:.0f}]"
            if a["per_1k_lo"] != a["per_1k_hi"]
            else str(a["per_1k_point"])
        ),
    }
    report["comparison"][arm] = {
        "rich_incremental": rich["arms"][arm]["incremental_paise"],
        "poor_incremental": poor["arms"][arm]["incremental_paise"],
        "delta": rich["arms"][arm]["incremental_paise"]
        - poor["arms"][arm]["incremental_paise"],
    }

# Power analysis from EVALUATION.md §3
per_case_sd = 12_870   # rupees of noise around per-case effect
effect_size = 315      # per-case effect size (also from EVALUATION.md)
treated_fraction = 0.75
holdout_fraction = 0.10
factor = 1 / (1 - holdout_fraction) + 1 / holdout_fraction
cases_needed = math.ceil(factor * (2.8 * per_case_sd / effect_size) ** 2)
report["power"] = {
    "per_case_sd_paise": per_case_sd,
    "effect_size_paise": effect_size,
    "cases_needed_for_80pct_power": cases_needed,
    "available_cases": 3_475,
    "shortfall_vs_80pct": "39×",
}

pathlib.Path("artifacts/demo-results.json").write_text(json.dumps(report, indent=2))
print("ok generated artifacts/demo-results.json")