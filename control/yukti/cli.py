"""Yukti control-plane CLI."""

from __future__ import annotations

import typer

from yukti.store import db

app = typer.Typer(add_completion=False, help="Yukti control plane")


@app.command()
def migrate() -> None:
    """Apply pending database migrations."""
    db.migrate()


@app.command("reset-db")
def reset_db(
    confirm: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Drop and recreate the schema, then re-migrate. Destructive."""
    if not confirm and not typer.confirm("Drop the entire public schema?"):
        raise typer.Abort()
    db.reset_schema()
    db.migrate()


@app.command()
def consume(
    group: str = typer.Option("yukti-opportunity", help="Kafka consumer group"),
    max_events: int = typer.Option(0, help="Stop after N events (0 = until idle)"),
) -> None:
    """Consume payment events and form recovery opportunities."""
    from yukti.opportunity.consumer import run

    run(group_id=group, max_events=max_events)


if __name__ == "__main__":
    app()
