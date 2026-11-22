"""What HTTP mode logs, and what it never logs.

A long-running server needs a record of who called what, and that record must
never hold the content it serves or credentials. Each tool call logs one line:
the tool, the caller's GitHub id, the outcome and the duration. Never arguments (a
search query is content), never results, never tokens.

Four leaks are closed by configuration rather than code:

- **HTTP client request logging.** A store's API token can ride in a URL's query
  string (Joplin's API takes it no other way), so httpx's INFO line for each
  request would print it. The HTTP client loggers are pinned to WARNING.
- **uvicorn's access log.** The OAuth callback URL carries the authorization code
  in its query string. The access log is off (see ``remote.run_http``).
- **FastMCP's debug log.** At DEBUG it prints every tool call with its arguments.
  Its loggers, and any a server names, are held at INFO or above.
- **FastMCP's error log.** When an exception escapes a tool, FastMCP logs "Error
  calling tool" with the traceback, and the exception's text with it, which can
  quote what was being processed. The traceback is dropped from that line; the
  tool's name stays, and the audit line names the class.
"""

import logging
import sys
import time
from collections.abc import Sequence
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware

from .server import Server

# Loggers that would print request URLs, and with them a token.
QUIET_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")
# Loggers that print each tool call's arguments at DEBUG. Arguments are content,
# so these stay at INFO or above.
NO_DEBUG_LOGGERS = ("fastmcp", "mcp")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
ANONYMOUS = "anonymous"
# Marks the handler configure_logging adds, so a second call adds none.
_HANDLER_MARK = "_second_brain_core"
# Where FastMCP logs a failed tool call (fastmcp/server/server.py).
FASTMCP_SERVER_LOGGER = "fastmcp.server.server"


class NoTraceback(logging.Filter):
    """Drops the traceback (and with it the exception's text) from a log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def outcome_name(exc: BaseException, named: tuple[type[BaseException], ...] = ()) -> str:
    """The class name the log gives a failed call: the cause, if it is a named one.

    A tool turns a server's own failures into a ``ToolError``; the log names the
    cause instead, so an operator can count outages from it. A class name only,
    never the message: messages can quote what the caller sent.
    """
    cause = exc.__cause__
    if named and isinstance(exc, ToolError) and isinstance(cause, named):
        return type(cause).__name__
    return type(exc).__name__


def caller_id(_context: MiddlewareContext[Any] | None = None) -> str:
    """The caller's GitHub user id, or ``anonymous`` with no sign-in."""
    token = get_access_token()
    if token is None:
        return ANONYMOUS
    user_id = token.claims.get("sub")
    return user_id if isinstance(user_id, str) and user_id else ANONYMOUS


class AuditMiddleware(Middleware):
    """Log one line per tool call: tool, caller, outcome, duration. Nothing else.

    ``named_causes`` are the server's own failure classes, logged by name (see
    :func:`outcome_name`).
    """

    def __init__(self, server: Server, named_causes: Sequence[type[BaseException]] = ()) -> None:
        self._log = server.logger("audit")
        self._named = tuple(named_causes)

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        tool = getattr(context.message, "name", "?")
        who = caller_id(context)
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            self._log.info(
                "tool=%s caller=%s outcome=error:%s ms=%d",
                tool,
                who,
                outcome_name(exc, self._named),
                (time.perf_counter() - started) * 1000,
            )
            raise
        outcome = "error" if getattr(result, "is_error", False) else "ok"
        self._log.info(
            "tool=%s caller=%s outcome=%s ms=%d",
            tool,
            who,
            outcome,
            (time.perf_counter() - started) * 1000,
        )
        return result


class ToolCallRateLimit(RateLimitingMiddleware):
    """FastMCP's token-bucket limiter, counting tool calls only.

    The stock middleware counts every MCP request, and one tool call from a client
    brings protocol requests with it (discovery, ``tools/list``), so a "60 a minute"
    limit would really mean a third of that. Listing and discovery are cheap; tool
    calls are what reach the store and the embedding model.
    """

    async def on_request(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await call_next(context)

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await super().on_request(context, call_next)


def rate_limit(per_minute: int) -> ToolCallRateLimit:
    """A per-caller token bucket of tool calls: ``per_minute`` sustained, bursts of half.

    Keyed by GitHub id (FastMCP's default is one bucket shared by every caller).
    """
    if per_minute < 1:
        raise ValueError("per_minute must be at least 1")
    return ToolCallRateLimit(
        max_requests_per_second=per_minute / 60,
        burst_capacity=max(1, per_minute // 2),
        get_client_id=caller_id,
    )


def configure_logging(
    server: Server, *, no_debug: Sequence[str] = (), level: int = logging.INFO
) -> None:
    """Logging for HTTP mode: the server's lines on stderr, URL loggers quiet.

    ``no_debug`` names more loggers that would print content at DEBUG (a protocol
    library's, say). Idempotent: calling it twice adds no second handler.
    """
    package = logging.getLogger(server.package)
    package.setLevel(level)
    if not any(getattr(h, _HANDLER_MARK, False) for h in package.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        setattr(handler, _HANDLER_MARK, True)
        package.addHandler(handler)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in (*NO_DEBUG_LOGGERS, *no_debug):
        chatty = logging.getLogger(name)
        if chatty.getEffectiveLevel() < logging.INFO:
            chatty.setLevel(logging.INFO)
    fastmcp_server = logging.getLogger(FASTMCP_SERVER_LOGGER)
    if not any(isinstance(f, NoTraceback) for f in fastmcp_server.filters):
        fastmcp_server.addFilter(NoTraceback())
