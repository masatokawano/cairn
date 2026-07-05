#!/usr/bin/env python
"""Launcher for the Cairn cross-source MCP server (M5).

Register this absolute path (not the package) so the invoking `python` puts
``backend/`` — this file's directory — on sys.path[0]. That makes ``import mcp``
inside the server resolve to the installed SDK, while ``app.mcp`` stays a
distinct dotted package. (The old ``app/mcp_server.py`` could not create an
``app/mcp/`` package because its script dir was ``app/``, where ``import mcp``
would have shadowed the SDK — NOTES.md 末尾「M0 逸脱」.)

    claude mcp add cairn -s user -- \
        /path/to/cairn/backend/.venv/bin/python /path/to/cairn/backend/run_mcp.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # backend/

from app.mcp.server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run()  # stdio transport (default)
