"""Turning a failure inside a tool into the error an MCP client sees.

Every tool ends the same way: a failure the server planned for becomes a
``ToolError`` with its own sentence (:func:`tool_error`), and anything else becomes
a ``ToolError`` with a fixed one (:func:`unexpected`). Build the server with
``mask_error_details=True`` as well, so an exception that escapes a tool anyway
reaches the client as FastMCP's generic sentence, never as its own text.
"""

import functools
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ParamSpec, TypeVar

from fastmcp.exceptions import ToolError

from .errors import FilterError
from .server import Server

P = ParamSpec("P")
R = TypeVar("R")


def tool_error(server: Server, exc: Exception) -> ToolError:
    """The ``ToolError`` for a planned failure, carrying its message to the client.

    A filter that doesn't resolve also gets a log line with the detail. The client
    is told only that the filter is wrong: the detail can name something the
    operator wants hidden.
    """
    if isinstance(exc, FilterError):
        server.logger("tools").warning("the %s doesn't resolve: %s", exc.WHAT, exc.detail)
    return ToolError(str(exc))


def internal_message(server: Server) -> str:
    """The sentence a client gets for a failure nobody planned for."""
    return f"{server.name} couldn't complete this request. The server's log says where."


def unexpected(server: Server, tool: str, exc: BaseException) -> ToolError:
    """The ``ToolError`` for a failure nobody planned for, with a fixed sentence.

    An exception's own text can hold what was being processed when it was raised:
    a note's text, a header, a URL with a token in it. So neither the client nor the
    log gets it. The log gets the exception's type and the place in the server's
    package where it surfaced, which is what a fix needs.
    """
    where = "outside the server's package"
    package_dir = server.package_dir()
    if package_dir is not None:
        for frame in reversed(traceback.extract_tb(exc.__traceback__)):
            path = Path(frame.filename).resolve()
            if package_dir in path.parents:
                where = f"{path.relative_to(package_dir)}:{frame.lineno} in {frame.name}"
                break
    server.logger("tools").error("unexpected %s in %s at %s", type(exc).__name__, tool, where)
    return ToolError(internal_message(server))


def guarded(
    server: Server,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """A tool decorator: a ``ToolError`` passes through; anything else is :func:`unexpected`.

    Put it under ``@mcp.tool``, so the tool keeps its own handling of the failures
    it plans for and nothing else escapes it::

        @mcp.tool(annotations=READ_ONLY)
        @guarded(SERVER)
        async def my_tool(...) -> ...: ...
    """

    def decorate(tool: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(tool)
        async def run(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await tool(*args, **kwargs)
            except ToolError:
                raise
            except Exception as exc:
                raise unexpected(server, tool.__name__, exc) from None

        return run

    return decorate
