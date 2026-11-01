#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp>=1.29.1,<2",
#     "requests>=2.32",
# ]
# ///
# ABOUTME: Read-only stdio entrypoint over the shared MCP read registry.
# ABOUTME: Hosted HTTP opts into the separately gated write-tool allowlist.
"""Run the read-only PRR MCP server over stdio.

Every tool this entrypoint registers calls only a GET method on ``PRRClient``.
Hosted HTTP opts into the separately gated draft/proposal tool allowlist and
supplies a per-request client carrying the caller's personal token. This stdio
entrypoint deliberately leaves that allowlist disabled and retains its
environment-configured personal or separately scoped read-only credential.
"""
from __future__ import annotations

import os
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from integrations.mcp.client import PRRClient
from integrations.mcp.tools import create_mcp_server

_client: PRRClient | None = None


def _client_for_context(_ctx):
    if _client is None:
        raise RuntimeError("The stdio MCP client has not been configured.")
    return _client


mcp = create_mcp_server(_client_for_context)


def _require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        print(
            f"ERROR: {name} is not set.\n\n"
            "This MCP server needs two environment variables:\n"
            "  PRR_BASE_URL   Base URL of the running Django app\n"
            "  PRR_API_TOKEN  A personal or read-only service token\n\n"
            "See integrations/mcp/README.md for setup instructions.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return value


def main():
    global _client
    _client = PRRClient(
        _require_env("PRR_BASE_URL"),
        _require_env("PRR_API_TOKEN"),
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
