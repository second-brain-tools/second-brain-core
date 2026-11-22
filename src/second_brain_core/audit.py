"""What HTTP mode logs, and what it never logs.

A long-running server needs a record of who called what, and that record must
never hold the content it serves or credentials. Each tool call logs one line:
the tool, the caller's GitHub id, the client it came from, the outcome and the
duration. Never arguments (a search query is content), never results, never tokens.

**The client's name** (``client=``) is what the client says about itself, so it is
a hint for the operator reading the log, never proof, and nothing is decided by it.
It is taken from the first of: the name the client registered under at sign-in, the
name it gives in the MCP handshake (or in each request, on protocols without one),
the first word of its ``User-Agent``. It keeps letters, digits and ``._/-`` only
(anything else becomes ``_``), at most 40 characters, so it can't break or forge a
line; with none of these it is ``unknown``. It names a sign-in, not an app: every
app that reaches the server through one connector shares its registration, and so
its name.

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
import re
import sys
import time
from collections import OrderedDict
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, get_http_headers
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
UNKNOWN = "unknown"
CLIENT_NAME_LENGTH = 40
_NOT_IN_A_NAME = re.compile(r"[^A-Za-z0-9._/-]+")
# The client of the tool call being handled, for the lines a tool logs itself.
_THIS_CALLS_CLIENT: ContextVar[str] = ContextVar("second_brain_core_client", default=UNKNOWN)
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


def clean_client_name(raw: object) -> str | None:
    """``raw`` as a short, safe word for the log, or None when nothing is left of it."""
    if not isinstance(raw, str):
        return None
    cleaned = _NOT_IN_A_NAME.sub("_", raw.strip()).strip("_")[:CLIENT_NAME_LENGTH]
    return cleaned.strip("_") or None


@runtime_checkable
class RegisteredClients(Protocol):
    """What knows the clients registered at sign-in (FastMCP's OAuth proxy does)."""

    async def get_client(self, client_id: str) -> Any: ...


class ClientNames:
    """The names clients registered under at sign-in, remembered by client id.

    A registration doesn't change its name, so each id is looked up once; at most
    ``at_most`` ids are kept.
    """

    def __init__(self, registry: RegisteredClients, at_most: int = 64) -> None:
        self._registry = registry
        self._at_most = at_most
        self._known: OrderedDict[str, str | None] = OrderedDict()

    async def name_of(self, client_id: str) -> str | None:
        if client_id in self._known:
            return self._known[client_id]
        try:
            client = await self._registry.get_client(client_id)
        except Exception:
            return None  # not remembered: the next call asks again
        name = clean_client_name(getattr(client, "client_name", None))
        self._known[client_id] = name
        while len(self._known) > self._at_most:
            self._known.popitem(last=False)
        return name


async def client_name(context: MiddlewareContext[Any], names: ClientNames | None = None) -> str:
    """The calling client's name for the log; see the module's docstring."""
    token = get_access_token()
    if names is not None and token is not None and token.client_id:
        registered = await names.name_of(token.client_id)
        if registered:
            return registered
    fastmcp_context = context.fastmcp_context
    try:
        params = fastmcp_context.session.client_params if fastmcp_context else None
    except RuntimeError:  # no session (yet)
        params = None
    info = getattr(params, "client_info", None)
    told = clean_client_name(getattr(info, "name", None))
    if told:
        return told
    agent = get_http_headers().get("user-agent", "").split()
    from_agent = clean_client_name(agent[0]) if agent else None
    return from_agent or UNKNOWN


def this_calls_client() -> str:
    """The client of the tool call being handled, for a line a tool logs itself."""
    return _THIS_CALLS_CLIENT.get()


class AuditMiddleware(Middleware):
    """Log one line per tool call: tool, caller, client, outcome, duration. Nothing else.

    ``named_causes`` are the server's own failure classes, logged by name (see
    :func:`outcome_name`). ``client_names`` looks up the name a client registered
    under at sign-in (HTTP mode with sign-in).
    """

    def __init__(
        self,
        server: Server,
        named_causes: Sequence[type[BaseException]] = (),
        client_names: ClientNames | None = None,
    ) -> None:
        self._log = server.logger("audit")
        self._named = tuple(named_causes)
        self._names = client_names

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        # The tool's name is what the client asked for, so it is cleaned like the
        # client's: an unknown name with a line break must not forge a line.
        tool = clean_client_name(getattr(context.message, "name", None)) or "?"
        who = caller_id(context)
        client = await client_name(context, self._names)
        token = _THIS_CALLS_CLIENT.set(client)
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            self._log.info(
                "tool=%s caller=%s client=%s outcome=error:%s ms=%d",
                tool,
                who,
                client,
                outcome_name(exc, self._named),
                (time.perf_counter() - started) * 1000,
            )
            raise
        finally:
            _THIS_CALLS_CLIENT.reset(token)
        outcome = "error" if getattr(result, "is_error", False) else "ok"
        self._log.info(
            "tool=%s caller=%s client=%s outcome=%s ms=%d",
            tool,
            who,
            client,
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
