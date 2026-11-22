"""The fake server itself (its package's ``__init__`` pins FastMCP first)."""

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from second_brain_core.annotations import READ_ONLY
from second_brain_core.audit import RegisteredClients, this_calls_client
from second_brain_core.errors import FilterError
from second_brain_core.failure import tool_error, unexpected
from second_brain_core.remote import build_middleware
from second_brain_core.server import Server

SERVER = Server(name="fake-server", prefix="SBF_", package="fakeserver", default_port=8799)
# A second description, to show nothing in the library is tied to one prefix.
OTHER = Server(name="other-server", prefix="SBO_", package="otherserver", default_port=8798)


class StoreDownError(Exception):
    """The fake store isn't answering: one of the server's own, named failures."""


class FakeFilterError(FilterError):
    MESSAGE = "The fake filter is wrong. The server's log says which entry."
    WHAT = "fake filter"


NAMED_CAUSES = (StoreDownError, FakeFilterError)


def create(
    *,
    auth: AuthProvider | None = None,
    allowed_ids: frozenset[str] | None = None,
    rate_limit_per_minute: int = 0,
    named_causes: tuple[type[BaseException], ...] = NAMED_CAUSES,
    clients: RegisteredClients | None = None,
) -> FastMCP:
    middleware = build_middleware(
        SERVER,
        allowed_ids=allowed_ids,
        rate_limit_per_minute=rate_limit_per_minute,
        named_causes=named_causes,
        signed_in=auth is not None,
        clients=clients,
    )
    server = FastMCP(SERVER.name, auth=auth, middleware=middleware, mask_error_details=True)

    @server.tool(annotations=READ_ONLY)
    async def fake_echo(text: str) -> str:
        """Say ``text`` back."""
        return text

    @server.tool(annotations=READ_ONLY)
    async def fake_whoami() -> str:
        """The client name the audit line gives this call, as a tool sees it."""
        return this_calls_client()

    @server.tool(annotations=READ_ONLY)
    async def fake_down() -> str:
        """Fail the way a store that is down fails."""
        try:
            raise StoreDownError("the fake store at http://127.0.0.1:1?token=SECRET is down")
        except StoreDownError as exc:
            raise tool_error(SERVER, exc) from exc

    @server.tool(annotations=READ_ONLY)
    async def fake_filter() -> str:
        """Fail the way a filter that doesn't resolve fails."""
        try:
            raise FakeFilterError("SBF_FILTER names 'Hidden Thing', which doesn't exist")
        except FakeFilterError as exc:
            raise tool_error(SERVER, exc) from exc

    @server.tool(annotations=READ_ONLY)
    async def fake_boom() -> str:
        """Fail in a way nobody planned for, with content in the exception's text."""
        try:
            return {"a": "1"}["the text of a private note"]
        except Exception as exc:
            raise unexpected(SERVER, "fake_boom", exc) from None

    @server.tool(annotations=READ_ONLY)
    async def fake_escape() -> str:
        """Let an unplanned exception escape the tool, as a forgotten case would."""
        raise RuntimeError("the text of a private note")

    return server
