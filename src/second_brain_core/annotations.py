"""MCP tool annotations every server uses the same way.

Clients use these hints, e.g. to skip a confirmation prompt for a read-only tool.
"""

from mcp.types import ToolAnnotations

# Reads the store, changes nothing, and touches nothing beyond this machine.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Asks the client to have a person approve every call of a tool, as tool metadata.
# Claude's clients read it; others ignore it. A server lets the operator turn it
# off, and then the check on each call is whatever the client does for any tool
# that writes.
ASK_EVERY_TIME = {"anthropic/requiresUserInteraction": True}
