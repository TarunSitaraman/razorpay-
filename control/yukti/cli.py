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


if __name__ == "__main__":
    app()
