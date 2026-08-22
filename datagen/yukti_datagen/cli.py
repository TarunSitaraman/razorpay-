"""Synthetic data CLI: generate, inspect, replay."""

from __future__ import annotations

import time

import typer
from rich.console import Console
from rich.table import Table
from yukti.config import settings

from yukti_datagen import persist
from yukti_datagen.generate import generate as _generate

app = typer.Typer(add_completion=False, help="Yukti synthetic data")
console = Console()


@app.command()
def generate(
    days: int = typer.Option(90, help="Days of history to simulate"),
    customers: int = typer.Option(1500, help="Customers per merchant"),
    seed: int = typer.Option(None, help="Override the default seed"),
) -> None:
    """Generate the synthetic world and write it to Postgres + Parquet."""
    seed = seed if seed is not None else settings().seed
    t0 = time.time()
    console.print(f"[cyan]generating[/] {days}d, {customers} customers/merchant, seed={seed}")
    ds = _generate(seed=seed, days=days, customers_per_merchant=customers)

    counts = persist.to_postgres(ds)
    path = persist.to_parquet(ds)

    t = Table(show_header=True, header_style="bold")
    t.add_column("entity")
    t.add_column("rows", justify="right")
    for k, v in counts.items():
        t.add_row(k, f"{v:,}")
    t.add_row("[bold]events (parquet)", f"[bold]{len(ds.events):,}")
    console.print(t)

    failed = sum(1 for a in ds.attempts if a["status"] == "failed")
    console.print(
        f"  failure rate [bold]{failed / max(1, len(ds.attempts)):.1%}[/]  "
        f"downtime windows [bold]{len(ds.downtime)}[/]  "
        f"degradations [bold]{len(ds.degradations)}[/]"
    )
    console.print(f"  wrote [green]{path}[/] in {time.time() - t0:.1f}s")


@app.command()
def replay(
    speed: float = typer.Option(200.0, help="Replay speed multiplier (0 = as fast as possible)"),
    limit: int = typer.Option(0, help="Stop after N events (0 = all)"),
) -> None:
    """Replay the Parquet event log into Kafka in timestamp order."""
    from yukti_datagen.replay import replay as _replay

    _replay(speed=speed, limit=limit)


@app.command("replay-webhooks")
def replay_webhooks_cmd(
    sandbox: str = typer.Option("http://localhost:8081", help="Sandbox base URL"),
    speed: float = typer.Option(0.0, help="Replay speed multiplier (0 = as fast as possible)"),
    limit: int = typer.Option(0, help="Stop after N events (0 = all)"),
) -> None:
    """Replay through the sandbox as signed webhooks (sandbox -> edge -> Kafka)."""
    from yukti_datagen.replay_webhooks import replay_webhooks

    replay_webhooks(sandbox_url=sandbox, speed=speed, limit=limit)


if __name__ == "__main__":
    app()
