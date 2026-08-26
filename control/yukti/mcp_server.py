import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from yukti.api import queries
from yukti.config import settings

import psycopg_pool
from psycopg.rows import dict_row

pool = psycopg_pool.ConnectionPool(
    settings().database_url, min_size=1, max_size=4, open=True,
    kwargs={"row_factory": dict_row},
)

def _q(fn, *args, **kwargs):
    with pool.connection() as conn:
        return fn(conn, *args, **kwargs)

server = Server("yukti-mcp")

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_revenue_at_risk",
            description="Get the total revenue at risk grouped by surface.",
            inputSchema={
                "type": "object",
                "properties": {
                    "merchant_id": {"type": "string", "description": "Optional merchant ID"}
                }
            }
        ),
        Tool(
            name="get_pipeline_counts",
            description="Get pipeline open case counts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "merchant_id": {"type": "string", "description": "Optional merchant ID"}
                }
            }
        ),
        Tool(
            name="get_stopping_rules",
            description="Get the amount of money deliberately not chased, split by reason.",
            inputSchema={
                "type": "object",
                "properties": {
                    "merchant_id": {"type": "string", "description": "Optional merchant ID"}
                }
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    merchant_id = arguments.get("merchant_id")
    if name == "get_revenue_at_risk":
        data = _q(queries.revenue_at_risk, merchant_id)
        return [TextContent(type="text", text=str(data))]
    elif name == "get_pipeline_counts":
        data = _q(queries.pipeline_counts, merchant_id)
        return [TextContent(type="text", text=str(data))]
    elif name == "get_stopping_rules":
        data = _q(queries.stop_reason_breakdown, merchant_id)
        return [TextContent(type="text", text=str(data))]
    else:
        raise ValueError(f"Unknown tool: {name}")

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
