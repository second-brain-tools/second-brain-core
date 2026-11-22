"""What HTTP mode logs (and never logs), the rate limit, and the error a client sees."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fakeserver import SERVER, FakeFilterError, StoreDownError, create
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.utilities.tests import ASGIServer, asgi_server
from mcp.shared.exceptions import MCPError

from second_brain_core.audit import (
    FASTMCP_SERVER_LOGGER,
    NO_DEBUG_LOGGERS,
    QUIET_LOGGERS,
    NoTraceback,
    configure_logging,
    outcome_name,
    rate_limit,
)
from second_brain_core.failure import internal_message
from second_brain_core.remote import HttpOptions, http_app_kwargs

SECRET_TEXT = "text-SECRET-3"
TOKENS = {
    "john": {"client_id": "c", "scopes": [], "sub": "65598"},
    "other": {"client_id": "c", "scopes": [], "sub": "2"},
}


@pytest.fixture(autouse=True)
def _restore_loggers() -> Any:
    names = (
        "fakeserver",
        "extra.protocol",
        FASTMCP_SERVER_LOGGER,
        *QUIET_LOGGERS,
        *NO_DEBUG_LOGGERS,
    )
    saved = {
        n: (
            logging.getLogger(n).level,
            list(logging.getLogger(n).handlers),
            list(logging.getLogger(n).filters),
        )
        for n in names
    }
    yield
    for name, (level, handlers, filters) in saved.items():
        logging.getLogger(name).setLevel(level)
        logging.getLogger(name).handlers[:] = handlers
        logging.getLogger(name).filters[:] = filters


@asynccontextmanager
async def serving(rate: int = 0) -> AsyncIterator[ASGIServer]:
    server = create(
        auth=StaticTokenVerifier(tokens=TOKENS),
        allowed_ids=frozenset({"65598", "2"}),
        rate_limit_per_minute=rate,
    )
    options = HttpOptions(host="127.0.0.1", port=8799, public_url="https://notes.example.com")
    kwargs = http_app_kwargs(options)
    path = kwargs.pop("path")
    async with asgi_server(server, path=path, **kwargs) as running:
        yield running


def audit_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "fakeserver.audit"]


def outcomes(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [line.split(" outcome=")[1].split(" ")[0] for line in audit_lines(caplog)]


def test_configure_logging_quietens_url_loggers_and_is_idempotent() -> None:
    configure_logging(SERVER)
    configure_logging(SERVER)
    for name in QUIET_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
    ours = [
        h for h in logging.getLogger("fakeserver").handlers if getattr(h, "_second_brain_core", 0)
    ]
    assert len(ours) == 1


def test_configure_logging_holds_argument_printing_loggers_at_info() -> None:
    for name in (*NO_DEBUG_LOGGERS, "extra.protocol"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    configure_logging(SERVER, no_debug=("extra.protocol",))
    for name in (*NO_DEBUG_LOGGERS, "extra.protocol"):
        assert logging.getLogger(name).level == logging.INFO
    # A quieter level is the operator's choice and is left alone.
    logging.getLogger("fastmcp").setLevel(logging.ERROR)
    configure_logging(SERVER)
    assert logging.getLogger("fastmcp").level == logging.ERROR


async def test_fastmcp_at_debug_would_print_tool_arguments() -> None:
    """The control: at DEBUG, FastMCP logs each call with its arguments, which are
    content. configure_logging keeps its loggers out of DEBUG."""
    printed: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            printed.append(record.getMessage())

    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.addHandler(Collect(level=logging.DEBUG))

    async def echo() -> None:
        async with Client(create()) as client:
            await client.call_tool("fake_echo", {"text": SECRET_TEXT})

    fastmcp_logger.setLevel(logging.DEBUG)
    await echo()
    assert any(SECRET_TEXT in line for line in printed)  # the leak is real

    printed.clear()
    configure_logging(SERVER)
    await echo()
    assert not any(SECRET_TEXT in line for line in printed)


async def test_logs_hold_the_call_but_no_content_or_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging(SERVER)
    caplog.set_level(logging.INFO)
    async with serving() as running, running.client(auth="john") as client:
        await client.call_tool("fake_echo", {"text": SECRET_TEXT})
    (line,) = audit_lines(caplog)
    assert line.startswith("tool=fake_echo caller=65598 outcome=ok ms=")
    for secret in (SECRET_TEXT, "Bearer john"):
        assert secret not in caplog.text, secret


async def test_a_failure_of_the_servers_own_is_logged_by_its_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="fakeserver")
    async with serving() as running, running.client(auth="john") as client:
        for tool in ("fake_down", "fake_filter", "fake_boom", "fake_escape"):
            result = await client.call_tool(tool, {}, raise_on_error=False)
            assert result.is_error
    assert outcomes(caplog) == [
        "error:StoreDownError",
        "error:FakeFilterError",
        "error:ToolError",  # unexpected() raises from None: nothing to name
        "error:ToolError",  # escaped: FastMCP masked it into a ToolError first
    ]
    assert "private note" not in " ".join(audit_lines(caplog))


def test_outcome_names_only_the_named_causes() -> None:
    def raised_from(cause: BaseException | None) -> ToolError:
        error = ToolError("a message that may quote the caller")
        error.__cause__ = cause
        return error

    named = (StoreDownError, FakeFilterError)
    assert outcome_name(raised_from(StoreDownError("x")), named) == "StoreDownError"
    assert outcome_name(raised_from(FakeFilterError("detail")), named) == "FakeFilterError"
    assert outcome_name(raised_from(StoreDownError("x"))) == "ToolError"  # none named
    assert outcome_name(raised_from(None), named) == "ToolError"
    assert outcome_name(raised_from(KeyError("x")), named) == "ToolError"  # not named
    assert outcome_name(ValueError("x"), named) == "ValueError"


async def test_rate_limit_is_per_caller() -> None:
    async with serving(rate=6) as running:  # bursts of 3 tool calls per caller
        seen = []
        async with running.client(auth="john") as client:
            assert await client.list_tools()  # listing is never counted
            for _ in range(5):
                try:
                    await client.call_tool("fake_echo", {"text": "q"})
                    seen.append("ok")
                except MCPError as exc:
                    assert "Rate limit exceeded for client: 65598" in str(exc)
                    seen.append("limited")
        async with running.client(auth="other") as client:  # a separate bucket
            other = await client.call_tool("fake_echo", {"text": "q"})
    assert seen == ["ok", "ok", "ok", "limited", "limited"]
    assert other.is_error is False


def test_rate_limit_needs_a_positive_rate() -> None:
    with pytest.raises(ValueError):
        rate_limit(0)


# -- the error a client sees ---------------------------------------------------


async def errors_of(*tools: str) -> list[str]:
    texts = []
    async with Client(create()) as client:
        for tool in tools:
            result = await client.call_tool(tool, {}, raise_on_error=False)
            assert result.is_error
            texts.append(" ".join(getattr(part, "text", "") for part in result.content))
    return texts


async def test_a_planned_failure_says_its_own_sentence() -> None:
    (down,) = await errors_of("fake_down")
    assert down == "the fake store at http://127.0.0.1:1?token=SECRET is down"


async def test_a_filter_error_tells_the_client_nothing_and_the_log_everything(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="fakeserver")
    (text,) = await errors_of("fake_filter")
    assert text == FakeFilterError.MESSAGE
    assert "Hidden Thing" not in text
    (line,) = [r.getMessage() for r in caplog.records if r.name == "fakeserver.tools"]
    assert line.startswith("the fake filter doesn't resolve: SBF_FILTER names 'Hidden Thing'")


async def test_an_unplanned_failure_gives_a_fixed_sentence_and_logs_where(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="fakeserver")
    (text,) = await errors_of("fake_boom")
    assert text == internal_message(SERVER)
    assert text == "fake-server couldn't complete this request. The server's log says where."
    (line,) = [r.getMessage() for r in caplog.records if r.name == "fakeserver.tools"]
    assert line.startswith("unexpected KeyError in fake_boom at app.py:")
    assert line.endswith(" in fake_boom")
    assert "private note" not in caplog.text


async def test_an_exception_that_escapes_a_tool_reaches_the_client_masked() -> None:
    """The second line of defence: mask_error_details. Without it FastMCP would send
    the exception's own text."""
    (text,) = await errors_of("fake_escape")
    assert "private note" not in text


async def test_an_escaped_exceptions_text_never_reaches_the_log() -> None:
    """FastMCP logs an escaped exception with its traceback, text and all (the
    control); configure_logging drops the traceback and keeps the line."""
    logged: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logged.append(self.format(record))

    fastmcp_server = logging.getLogger(FASTMCP_SERVER_LOGGER)
    fastmcp_server.addHandler(Collect(level=logging.ERROR))
    await errors_of("fake_escape")
    assert any("private note" in line for line in logged)  # the leak is real

    logged.clear()
    configure_logging(SERVER)
    configure_logging(SERVER)
    assert sum(isinstance(f, NoTraceback) for f in fastmcp_server.filters) == 1
    await errors_of("fake_escape")
    assert logged == ["Error calling tool 'fake_escape'"]
