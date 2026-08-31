"""The MCP surface must import, list its tools, and stay read-only.

It shipped broken: written against the `mcp` 1.x decorator API, it raised
`AttributeError` at import under the version the project pins, so "a functioning
MCP server" was a claim no reviewer could reproduce. An import test is cheap and
would have caught it.

The second assertion is the one that matters for the thesis. Everything Niyama
can do to a customer's money goes through the dispatcher behind the policy
engine. An MCP tool that could act would be a second, unpoliced write path.
"""

from __future__ import annotations

import asyncio
import os

from yukti import mcp_server

# Read models the console already serves. Anything outside this set is a new
# capability and should be a deliberate decision, not a drive-by addition.
EXPECTED_TOOLS = {
    "get_revenue_at_risk",
    "get_pipeline_counts",
    "get_stopping_rules",
    "get_policy_blocks",
}

# Verbs that would mean the surface can change something.
ACTING_VERBS = ("send", "dispatch", "retry", "refund", "charge", "cancel",
                "create", "update", "delete", "approve", "plan", "schedule")


def test_the_tools_are_the_read_models():
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}

    assert names == EXPECTED_TOOLS


def test_no_tool_can_act():
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}

    offenders = [n for n in names if any(v in n for v in ACTING_VERBS)]
    assert offenders == [], f"MCP tools must be read-only; found {offenders}"


def test_importing_the_module_does_not_open_a_database_connection():
    """Listing tools is how a client discovers the server, so import and
    discovery have to work before anyone has a stack running. Checked in a
    fresh interpreter pointed at a database that does not exist."""
    import subprocess
    import sys

    env = {**os.environ, "YUKTI_DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none"}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import asyncio; from yukti import mcp_server;"
         "assert mcp_server._pool is None;"
         "print(len(asyncio.run(mcp_server.server.list_tools())))"],
        capture_output=True, text=True, env=env, timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(len(EXPECTED_TOOLS))
