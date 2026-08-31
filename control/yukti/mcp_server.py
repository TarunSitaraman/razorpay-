"""MCP surface over the console's read models.

Peripheral to the thesis and named as such in the README: it exposes the same
read-only queries the console draws, so an agent can ask what is at risk, what
is in flight and what was deliberately not chased. It cannot act. The write path
is the dispatcher behind the policy engine, and nothing here reaches it.

Written against the `mcp` 2.x server API. The previous version used the 1.x
decorator surface (`mcp.server.Server.list_tools`), which no longer exists — so
the module raised `AttributeError` at import and the "functioning MCP server"
claim was false for anyone who installed the dependency as pinned.
"""

from __future__ import annotations

from typing import Any

import psycopg_pool
from mcp.server.mcpserver import MCPServer
from psycopg.rows import dict_row

from yukti.api import queries
from yukti.config import settings

server = MCPServer(
    "niyama",
    instructions=(
        "Read-only analytics over Niyama's recovery book. Every tool answers a "
        "question about money and none of them can start, stop or alter a "
        "recovery action."
    ),
)

_pool: psycopg_pool.ConnectionPool | None = None


def _q(fn, *args: Any, **kwargs: Any) -> Any:
    """Opened lazily: importing this module must not require a database."""
    global _pool
    if _pool is None:
        _pool = psycopg_pool.ConnectionPool(
            settings().database_url, min_size=1, max_size=4, open=True,
            kwargs={"row_factory": dict_row},
        )
    with _pool.connection() as conn:
        return fn(conn, *args, **kwargs)


@server.tool()
def get_revenue_at_risk(merchant_id: str | None = None) -> dict:
    """Open recoverable money, split by surface (cart, subscription, invoice, order)."""
    return _q(queries.revenue_at_risk, merchant_id)


@server.tool()
def get_pipeline_counts(merchant_id: str | None = None) -> dict:
    """How many cases are open, awaiting outcome, stopped, or held out."""
    return _q(queries.pipeline_counts, merchant_id)


@server.tool()
def get_stopping_rules(merchant_id: str | None = None) -> list[dict]:
    """Money deliberately not chased, grouped by the named rule that stopped it."""
    return _q(queries.stop_reason_breakdown, merchant_id)


@server.tool()
def get_policy_blocks(merchant_id: str | None = None) -> list[dict]:
    """Actions a policy rule refused, by rule — the compliance side of the same book."""
    return _q(queries.refused_alternatives, merchant_id)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
