"""Render `artifacts/sensitivity.json` as the markdown tables the docs embed.

Exists so the frontier in README.md and EVALUATION.md is generated from the run
rather than transcribed from it. Transcribed numbers drift from the code that
produced them, and a drifted number in the one section whose entire purpose is
"do not take my word for it" would be worse than having no section.

    python scripts/frontier_markdown.py            # all axes
    python scripts/frontier_markdown.py --axis persuadable_uplift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode the characters used in
# the tables below. A full sweep takes tens of minutes; the first version of the
# sweep command lost an entire run to exactly this, so reconfigure rather than
# rely on the ambient codepage.
sys.stdout.reconfigure(encoding="utf-8")

ARMS = ("B1", "B3", "Y")
LABELS = {"B1": "fixed cadence", "B3": "propensity", "Y": "**Niyama**"}
ARCHETYPES = ("persuadable", "sure_thing", "lost_cause", "sleeping_dog")


def rupees(paise: int) -> str:
    return f"{paise / 100:,.0f}"


def fails_when(block: dict) -> str:
    """Which side of the crossover the thesis stops surviving on.

    Derived from the grid rather than read from a field, so this works on
    artifacts written by older runs and cannot disagree with the numbers printed
    directly above it.

    The logic has to account for grid ORIENTATION, not just which end wins.
    `persuadable_uplift` descends (0.46 -> 0.03) and wins at the start, so it
    fails below. `sure_thing_uplift` ascends (0.04 -> 0.34) and also wins at the
    start, so it fails ABOVE. Keying only on "does the first point win" gets the
    second one backwards, in the direction that reads as a stronger result than
    it is.
    """
    points = block["points"]
    if len(points) < 2:
        return "below"
    first_wins = points[0]["margin_over_rival_paise"] > 0
    # The failing end is whichever end the thesis does NOT survive at.
    failing = points[-1] if first_wins else points[0]
    surviving = points[0] if first_wins else points[-1]
    return "below" if failing["value"] < surviving["value"] else "above"


def money_table(axis: str, block: dict) -> str:
    lines = [
        f"| `{axis}` | fixed cadence | propensity | **Niyama** | winner |",
        "|---|--:|--:|--:|---|",
    ]
    for pt in block["points"]:
        cells = [rupees(pt["arms"][a]["contact_attributable_paise"]) for a in ARMS]
        cells[2] = f"**{cells[2]}**"
        winner = "**Niyama**" if pt["winner"] == "Y" else {
            "B4": "retry-only (nobody should contact)",
            "B1": "fixed cadence",
            "B3": "propensity",
        }.get(pt["winner"], pt["winner"])
        lines.append(f"| {pt['value']:g} | {' | '.join(cells)} | {winner} |")
    return "\n".join(lines)


def targeting_table(block: dict) -> str:
    lines = [
        "| assumption | arm | persuadable | sure thing | lost cause | sleeping dog |",
        "|---|---|--:|--:|--:|--:|",
    ]
    for pt in block["points"]:
        for arm in ARMS:
            mix = pt["arms"][arm].get("contacted_by_archetype", {})
            counts = " | ".join(str(mix.get(k, 0)) for k in ARCHETYPES)
            lines.append(f"| {pt['value']:g} | {LABELS[arm]} | {counts} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="artifacts/sensitivity.json")
    ap.add_argument("--axis", default=None)
    ap.add_argument("--targeting", action="store_true",
                    help="also emit the contact-mix table")
    args = ap.parse_args()

    data = json.loads(Path(args.path).read_text(encoding="utf-8"))
    axes = [args.axis] if args.axis else list(data)

    for axis in axes:
        block = data[axis]
        print(f"\n#### `{axis}`\n")
        print(block["description"], "\n")
        print(money_table(axis, block))
        x = block["crossover"]
        if x is None:
            print("\n*No crossover in the swept range.*")
        else:
            side = fails_when(block)
            print(
                "\n**Crossover \u2248 " + f"{x:.3f}" + "** \u2014 "
                + side + " this the uplift objective no longer pays for itself."
            )
        if args.targeting:
            print()
            print(targeting_table(block))


if __name__ == "__main__":
    main()
