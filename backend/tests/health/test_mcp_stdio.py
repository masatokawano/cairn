"""H7 process-level smoke test for the opt-in STDIO MCP surface.

Synthetic data only.  This launches ``run_health_mcp.py`` exactly as a client
registration does, then performs MCP initialize/list/call over stdio.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.health.importers import labs_csv


@pytest.fixture
def stdio_health(health_home, catalog_dir, labs_csv_path):
    labs_csv.run(labs_csv_path, catalog_dir=catalog_dir)
    return health_home


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_opted_in_stdio_server_lists_and_calls_bounded_tool(stdio_health):
    backend = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.update({
        "CAIRN_HEALTH_MCP": "1",
        "CAIRN_HEALTH_HOME": str(stdio_health),
    })
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(backend / "run_health_mcp.py")],
        cwd=backend,
        env=env,
    )

    with anyio.fail_after(10):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert "health_query_observations" in names
                result = await session.call_tool(
                    "health_query_observations",
                    {"metrics": ["synthetic_a"], "max_rows": 1},
                )

    assert result.isError is False
    payload = result.structuredContent
    if payload is None:
        payload = json.loads(result.content[0].text)
    assert payload["row_count"] == 1
    assert payload["capped_at"] == 1
    assert payload["metrics"] == ["synthetic_a"]
