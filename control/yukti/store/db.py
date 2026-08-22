"""Postgres access and the migration runner.

Migrations are plain numbered .sql files applied in order inside a transaction,
with applied versions recorded in `schema_migration`. Alembic's autogenerate is
not useful here — the schema carries partial unique indexes, CHECK constraints
and an append-only hash chain that are the point of the design, and hand-written
DDL keeps them reviewable.
"""

from __future__ import annotations

import pathlib

import psycopg
from psycopg.rows import dict_row

from yukti.config import settings

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "migrations"


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(settings().database_url, row_factory=dict_row, autocommit=autocommit)


def _ensure_migration_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version     TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def applied_versions(conn: psycopg.Connection) -> set[str]:
    _ensure_migration_table(conn)
    return {r["version"] for r in conn.execute("SELECT version FROM schema_migration")}


def migrate(verbose: bool = True) -> list[str]:
    """Apply pending migrations in filename order. Returns what was applied."""
    applied: list[str] = []
    with connect() as conn:
        have = applied_versions(conn)
        conn.commit()
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in have:
                continue
            sql = path.read_text()
            # Each file manages its own BEGIN/COMMIT so that DDL grouping is
            # explicit in the file rather than implied by the runner.
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migration (version) VALUES (%s)", (version,)
            )
            conn.commit()
            applied.append(version)
            if verbose:
                print(f"  applied {version}")
    if verbose and not applied:
        print("  no pending migrations")
    return applied


def reset_schema() -> None:
    """Drop and recreate the public schema. Destructive; used by `make reset`."""
    with connect(autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
