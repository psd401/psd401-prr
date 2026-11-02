# Files

- [Staff Command-Line Client](cli.md) - The single-file token-authenticated CLI (integrations/cli/prr.py) over the staff REST API. It maps each command to a registered capability, adds no compliance logic, and renders human-only server handoffs with exit status 2. Read before changing a CLI command, its capability mapping, or its configuration checks.
- [MCP Server](mcp.md) - MCP access to the PSD401 PRR staff API for LLM clients. Standalone stdio server exposes eight read-only tools; the hosted /mcp endpoint adds four attributed draft and proposal tools that never approve, send, or change legal state.
