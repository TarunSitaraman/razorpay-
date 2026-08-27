"""`python -m yukti.eval.cli run` — the evaluation entry point."""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(add_completion=False, help="Yukti evaluation harness")
console = Console()


@app.command()
def run(
    merchant: str = typer.Option(None, help="Merchant id; omit for the largest"),
    date: str = typer.Option(None, "--date", help="Planning moment, ISO"),
    limit: int = typer.Option(0, help="Cap cases per arm (0 = all)"),
    seed: int = typer.Option(0, help="Override the outcome seed (0 = configured)"),
    save: bool = typer.Option(
        True, help="Write artifacts/eval-report.json for the console"),
    contact_budget: int = typer.Option(
        0, help="Override the merchant's contact budget (0 = as configured)"),
) -> None:
    """Score all five arms on identical cases and print the comparison."""
    from datetime import UTC, datetime

    from yukti.config import settings
    from yukti.eval import report
    from yukti.eval.harness import run as run_eval
    from yukti.store.db import connect

    as_of = datetime.fromisoformat(date) if date else datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)

    with connect() as conn:
        if not merchant:
            # Counted over PLANNABLE cases — those the harness's reset will
            # reopen — rather than over `state = 'open'`, which reflects
            # whatever the last run left behind and made the choice of merchant
            # depend on run order.
            row = conn.execute(
                "SELECT merchant_id, count(*) AS n FROM recovery_case c "
                " WHERE NOT EXISTS (SELECT 1 FROM recovery_outcome o "
                "                    WHERE o.case_id = c.id) "
                " GROUP BY 1 ORDER BY n DESC, merchant_id LIMIT 1"
            ).fetchone()
            if row is None:
                console.print("[red]no open cases[/] — run "
                              "`make replay-fast && make consume` first")
                raise typer.Exit(1)
            merchant = row["merchant_id"]

        result = run_eval(conn, merchant, as_of,
                          limit=limit or None, seed=seed or settings().seed,
                          contact_budget=contact_budget or None)
        # Saved BEFORE rendering. A full run takes minutes, and the previous one
        # was killed between the last table and this line — losing the whole
        # result and leaving /metrics/lift returning 404 with nothing to show
        # for the compute.
        if save:
            path = report.save(result)
        report.render(result, console)
        if save:
            console.print(f"  [dim]written to {path}[/]")


@app.command()
def sweep(
    merchant: str = typer.Option(None, help="Merchant id; omit for the largest"),
    date: str = typer.Option(None, "--date", help="Planning moment, ISO"),
    budgets: str = typer.Option("0,45,90,250,750,2000",
                                help="Comma-separated contact budgets to sweep"),
    seed: int = typer.Option(0, help="Override the outcome seed (0 = configured)"),
) -> None:
    """Lift as a function of the contact budget.

    A single budget produces a single contested number, and on this dataset the
    contact budget is ~2% of the case count — so every arm spends all of it and
    the arms look nearly identical. Sweeping it answers the more useful and more
    defensible question: *when* does optimising for uplift actually pay?

    The shape to expect: at budget 0 every arm collapses onto retry-only and the
    spread is exactly zero. As the budget grows the arms must start choosing
    WHOM to contact rather than merely whether, and that is where a causal
    objective separates from a propensity one. Large enough, and everyone
    contacts everyone worth contacting and the spread closes again.
    """
    from datetime import UTC, datetime

    from rich.table import Table

    from yukti.config import settings
    from yukti.domain.money import format_inr
    from yukti.eval.arms import BY_KEY, REFERENCE
    from yukti.eval.harness import run as run_eval
    from yukti.store.db import connect

    as_of = datetime.fromisoformat(date) if date else datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    wanted = [int(b) for b in budgets.split(",") if b.strip()]

    table = Table(title="Contact-attributable margin by contact budget",
                  header_style="bold",
                  caption="Each arm against retry-only, which they all share. "
                          "At budget 0 every arm IS retry-only, so a non-zero "
                          "spread there would be a bug.")
    table.add_column("contact budget", justify="right")

    keys: list[str] = []
    with connect() as conn:
        if not merchant:
            # Counted over PLANNABLE cases — those the harness's reset will
            # reopen — rather than over `state = 'open'`, which reflects
            # whatever the last run left behind and made the choice of merchant
            # depend on run order.
            row = conn.execute(
                "SELECT merchant_id, count(*) AS n FROM recovery_case c "
                " WHERE NOT EXISTS (SELECT 1 FROM recovery_outcome o "
                "                    WHERE o.case_id = c.id) "
                " GROUP BY 1 ORDER BY n DESC, merchant_id LIMIT 1"
            ).fetchone()
            if row is None:
                console.print("[red]no open cases[/]")
                raise typer.Exit(1)
            merchant = row["merchant_id"]

        rows: list[tuple[int, dict[str, int]]] = []
        for b in wanted:
            console.print(f"  [dim]budget {b}...[/]")
            result = run_eval(conn, merchant, as_of, seed=seed or settings().seed,
                              contact_budget=b)
            if not keys:
                keys = [k for k in result.metrics
                        if k not in (REFERENCE.key, "B0")]
                for k in keys:
                    table.add_column(f"{k} {BY_KEY[k].label}", justify="right")
            rows.append((b, {
                k: (result.metrics[k].contact_incremental_margin_paise,
                    result.metrics[k].contacts) for k in keys
            }))

    # Contacts actually used are shown beside the margin. Without them a
    # saturating row reads as "the arms stopped responding to budget" when what
    # it means is "the budget stopped binding" — a different fact, and the one
    # that explains the shape.
    for b, by_key in rows:
        best = max(v for v, _ in by_key.values()) if by_key else 0
        cells = []
        for k in keys:
            margin, contacts = by_key[k]
            text = f"{format_inr(margin)}  ({contacts:,} used)"
            cells.append(f"[bold green]{text}[/]" if margin == best else text)
        table.add_row(f"{b:,}", *cells)
    console.print()
    console.print(table)


@app.command()
def arms() -> None:
    """Describe the arms and why each is there."""
    from rich.table import Table

    from yukti.eval.arms import ARMS

    table = Table(title="Evaluation arms", header_style="bold")
    table.add_column("arm"); table.add_column("policy"); table.add_column("why")
    for arm in ARMS:
        table.add_row(arm.key, arm.label, arm.description)
    console.print(table)
    console.print(
        "\n  [dim]Every arm runs the same allocator, stopping rules and policy "
        "engine. Only the number being optimised changes — so any difference in "
        "the result is the objective, never the plumbing.[/]\n"
    )




@app.command()
def sensitivity(
    axis: str = typer.Option(
        "all", help="Assumption to sweep, or 'all'. See yukti.eval.sensitivity.AXES"),
    n_train: int = typer.Option(
        20000,
        help="Exploration cases used to fit each model. Below ~10,000 the "
             "learner cannot rank the archetypes and the sweep measures "
             "under-training rather than the assumption being swept."),
    n_plan: int = typer.Option(3500, help="Cases in the planning population"),
    contact_budget: int = typer.Option(90, help="Contacts available per cycle"),
    seed: int = typer.Option(20260822, help="Outcome seed"),
    save: str = typer.Option(
        "artifacts/sensitivity.json", help="Where to write the raw grid"),
) -> None:
    """Sweep the assumptions the headline result rests on.

    Needs no database and no services: the world is generated, explored, learned
    and graded in process. That is deliberate — the frontier is the answer to
    "you built a world where you win", so it has to be reproducible by someone
    who has just cloned the repository and cannot run the stack.
    """
    import sys

    from yukti.eval import sensitivity as sens
    from yukti.eval.sensitivity_report import GRIDS, render, save_grid

    # Windows consoles default to cp1252 and cannot encode the characters rich
    # reaches for. A full sweep refits a model at ~28 grid points; the first run
    # of this command computed all of it and then died in the renderer on a
    # single minus sign. Reconfiguring costs nothing and the alternative is
    # losing an hour of compute to a codepage.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    axes = list(GRIDS) if axis == "all" else [axis]
    unknown = [a for a in axes if a not in sens.AXES]
    if unknown:
        console.print(f"[red]unknown axis[/]: {unknown[0]}. "
                      f"choose from {', '.join(sens.AXES)}")
        raise typer.Exit(1)

    results = {}
    for name in axes:
        console.print(f"[cyan]sweeping[/] {name} - refitting at each point")
        results[name] = sens.sweep(
            name, GRIDS[name], n_train=n_train, n_plan=n_plan,
            contact_budget=contact_budget, seed=seed,
        )
        # Written after EVERY axis, not once at the end. A full sweep refits a
        # model at each of ~29 grid points and takes tens of minutes; the first
        # run of this command computed all of it and then died in the renderer
        # on a Unicode minus sign under a cp1252 console, losing the lot. The
        # same lesson is already recorded one command up in this file.
        if save:
            save_grid(results, save)

    render(console, results)
    if save:
        console.print("\n[dim]grid written to " + str(save) + "[/]")


if __name__ == "__main__":
    app()
