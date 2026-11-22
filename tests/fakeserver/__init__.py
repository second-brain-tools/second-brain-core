"""A tiny MCP server built on second_brain_core, for the library's own tests.

It is a real package (``fakeserver``), so the pieces that look for the server's
package on disk (the checkout ``.env``, where an unexpected error surfaced) find it.
"""

# The pin must run before anything imports fastmcp, so this file imports nothing
# that could: import sorting would move `import fastmcp` above this line, which
# is the very mistake ``testing.check_dotenv_cannot_set_fastmcp`` catches.
import second_brain_core  # noqa: F401

from .app import NAMED_CAUSES, OTHER, SERVER, FakeFilterError, StoreDownError, create

__all__ = ["NAMED_CAUSES", "OTHER", "SERVER", "FakeFilterError", "StoreDownError", "create"]
