"""Rendering the assumption frontier.

Kept separate from `sensitivity.py` so the measurement has no opinion about
presentation, and so the grids below — which are the claim about what counts as
a *plausible* range — sit somewhere a reader can argue with them.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from yukti.eval.sensitivity import (
    ARM_LABELS,
    AXES,
    SweepPoint,
    crossover,
    crossover_direction,
)

# The swept ranges. Each is chosen to span from "the headline assumption" to
# "the assumption a sceptic would make", so the frontier brackets the argument
# rather than decorating it.
GRIDS: dict[str, tuple[float, ...]] = {
    # Headline assumes 0.46. Published uplift effects in retention marketing are
    # more often single-digit percentage points, so the low end is the honest
    # sceptic's number.
    "persuadable_uplift": (0.46, 0.34, 0.24, 0.16, 0.10, 0.06, 0.03),
    # Headline assumes 15% of the book. Zero is the position that sleeping dogs
    # are a marketing story.
    "sleeping_dog_share": (0.15, 0.11, 0.07, 0.04, 0.02, 0.0),
    # Headline assumes 0.04 — sure things have almost no headroom. As this rises
    # toward the persuadable's, propensity and uplift rank identically.
    "sure_thing_uplift": (0.04, 0.10, 0.18, 0.26, 0.34),
    # Headline assumes 0.0 — that a silent retry is free. It is not.
    "silent_retry_irritation": (0.0, 0.02, 0.05, 0.09, 0.15),
    # Headline assumes 0.78 per prior contact. 1.0 is "fatigue is not real".
    "fatigue_decay": (0.78, 0.85, 0.92, 0.97, 1.0),
}


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.0f}"


def render(console: Console, results: dict[str, list[SweepPoint]]) -> None:
    """Print one table per axis, plus the verdict."""
    for axis, points in results.items():
        table = Table(
            title=f"{axis}: {AXES[axis]}",
            title_style="bold",
            caption="contact-attributable margin (INR) vs retry-only, which every arm shares.\nThe free-retry mass cancels, so what is left is the contact decision alone.",
        )
        table.add_column(axis, justify="right")
        for arm in ("B1", "B3", "Y"):
            table.add_column(ARM_LABELS[arm], justify="right")
        table.add_column("Y - best rival", justify="right")
        table.add_column("winner")

        for p in points:
            cells = []
            for arm in ("B1", "B3", "Y"):
                r = p.arms[arm]
                style = "bold green" if p.winner == arm else ""
                text = _rupees(r.contact_attributable_paise)
                cells.append(f"[{style}]{text}[/]" if style else text)
            delta = p.margin_over_rival()
            verdict = "[green]Niyama[/]" if delta > 0 else f"[red]{ARM_LABELS[p.winner]}[/]"
            table.add_row(
                f"{p.value:g}", *cells,
                f"[{'green' if delta > 0 else 'red'}]{_rupees(delta)}[/]",
                verdict,
            )
        console.print(table)
        _render_targeting(console, points)

        x = crossover(points)
        if x is None:
            if all(p.margin_over_rival() > 0 for p in points):
                console.print(
                    f"  [yellow]no crossover in the swept range[/] - Niyama wins "
                    f"across all of {axis}. Widen the grid before believing it.\n")
            else:
                console.print(
                    f"  [red]Niyama loses across the whole swept range of {axis}.[/]\n")
        else:
            side = crossover_direction(points)
            console.print(
                f"  [bold]crossover at {axis} ~ {x:.3f}[/] - {side} this "
                f"the uplift objective no longer pays for itself.\n")


def _render_targeting(console: Console, points: list[SweepPoint]) -> None:
    """Who each arm actually spent its contact budget on.

    This is the mechanism table, and it is more informative than the money
    table. Margin confounds two things — did the arm target the right people,
    and were those people's obligations large. Targeting cannot be confounded
    that way, and it is where the propensity arm fails most visibly: it spends
    its budget on sure things and reaches almost no persuadables, which is the
    entire thesis stated as a count rather than as a claim.
    """
    table = Table(
        title="  who got contacted, by latent archetype (ground truth)",
        title_style="bold",
        caption="persuadable = the only archetype where contact creates value.\n"
                "sleeping_dog = contact actively destroys value.",
    )
    table.add_column("value", justify="right")
    table.add_column("arm")
    table.add_column("persuadable", justify="right")
    table.add_column("sure_thing", justify="right")
    table.add_column("lost_cause", justify="right")
    table.add_column("sleeping_dog", justify="right")

    for p in points:
        for arm in ("B1", "B3", "Y"):
            mix = p.arms[arm].contacted_by_archetype
            style = "bold green" if arm == "Y" else ""
            cells = [str(mix.get(k, 0)) for k in
                     ("persuadable", "sure_thing", "lost_cause", "sleeping_dog")]
            if style:
                cells = [f"[{style}]{c}[/]" for c in cells]
            table.add_row(f"{p.value:g}", ARM_LABELS[arm], *cells)
        table.add_section()
    console.print(table)


def save_grid(results: dict[str, list[SweepPoint]], path: str) -> Path:
    """Write the raw grid so the console and the docs read one source."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        axis: {
            "description": AXES[axis],
            "crossover": crossover(points),
            "fails_when": crossover_direction(points),
            "points": [
                {
                    "value": p.value,
                    "winner": p.winner,
                    "margin_over_rival_paise": p.margin_over_rival(),
                    "arms": {
                        arm: {
                            "contacts": r.contacts,
                            "recovered_cases": r.recovered_cases,
                            "opt_outs": r.opt_outs,
                            "incremental_paise": r.incremental_paise,
                            "contact_attributable_paise": r.contact_attributable_paise,
                            "contacted_by_archetype": r.contacted_by_archetype,
                            "per_1k_point": r.per_1k.point,
                            "per_1k_lo": r.per_1k.low,
                            "per_1k_hi": r.per_1k.high,
                        }
                        for arm, r in p.arms.items()
                    },
                }
                for p in points
            ],
        }
        for axis, points in results.items()
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out
