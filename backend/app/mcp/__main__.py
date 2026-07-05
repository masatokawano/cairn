"""`python -m app.mcp` entry point (run from backend/ so `import mcp` → SDK).

The registration form documented in the README uses the ``run_mcp.py``
launcher; this module makes the package directly runnable too.
"""
from .server import mcp

if __name__ == "__main__":
    mcp.run()
