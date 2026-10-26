"""``python -m gitlab_mcp`` entry point: run the MCP server over stdio.

Nothing is printed here. MCP speaks its protocol on stdout, so any stray
write to stdout corrupts the session.
"""

from __future__ import annotations

from gitlab_mcp import server


def _run() -> None:
    server.main()


if __name__ == "__main__":
    _run()
