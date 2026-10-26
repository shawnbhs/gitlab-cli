"""MCP server for the gitlab-cli toolbox.

This package exposes the existing gitlab-cli code base to an AI agent over the
Model Context Protocol:

* :mod:`gitlab_mcp.context` holds the per-instance API session.
* :mod:`gitlab_mcp.reporting` holds the read-only report functions.
* :mod:`gitlab_mcp.actions` holds every mutating operation, each as a
  side-effect-free ``preview_*`` plus a ``do_*``.
* :mod:`gitlab_mcp.confirm` holds the single-use confirmation-token store.
* :mod:`gitlab_mcp.server` wires all of it into a FastMCP server over stdio.

Reads are free. Every write, merge or local pull is a two-call flow: the first
call only returns a preview and a confirmation token, and the human has to
approve the preview before the second call can execute anything.

Run it with ``python -m gitlab_mcp`` from the gitlab-cli repository root.
"""

from __future__ import annotations

from version import __version__  # noqa: E402 - the single version source

__all__ = ["__version__"]
