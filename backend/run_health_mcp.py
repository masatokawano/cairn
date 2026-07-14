#!/usr/bin/env python
"""Launcher for the Cairn HEALTH MCP server (H7, ADR-0005).

Separate from run_mcp.py: the health MCP is opt-in and independently
disable-able. Registering it does nothing unless CAIRN_HEALTH_MCP is set —
the server refuses to start otherwise (PRIVACY.md §6).

    claude mcp add cairn-health -s user -e CAIRN_HEALTH_MCP=1 -- \
        /path/to/backend/.venv/bin/python /path/to/backend/run_health_mcp.py

Same sys.path trick as run_mcp.py so ``import mcp`` resolves to the SDK.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # backend/

from app.health.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
