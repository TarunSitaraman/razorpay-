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
                k: result.metrics[k].contact_incremental_margin_paise for k in keys
            }))

    for b, by_key in rows:
        best = max(by_key.values()) if by_key else 0
        table.add_row(
            f"{b:,}",
            *[f"[bold green]{format_inr(by_key[k])}[/]" if by_key[k] == best
              else format_inr(by_key[k]) for k in keys],
        )
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


if __name__ == "__main__":
    app()
