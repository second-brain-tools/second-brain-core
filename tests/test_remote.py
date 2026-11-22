"""HTTP mode: options, start-up guardrails, the transport, the middleware and /healthz."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fakeserver import OTHER, SERVER, create
from fastmcp import FastMCP
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.utilities.tests import ASGIServer, asgi_server

from second_brain_core.audit import AuditMiddleware, ToolCallRateLimit
from second_brain_core.errors import ServeConfigError
from second_brain_core.health import make_healthz
from second_brain_core.remote import (
    HttpOptions,
    build_middleware,
    choose_transport,
    http_app_kwargs,
    http_options,
    is_loopback,
    refuse_http_flags,
    run_http,
    run_stdio,
    validate,
)


def opts(**kwargs: Any) -> HttpOptions:
    """Options as the fake server's defaults give them, changed by ``kwargs``."""
    return HttpOptions(**{"host": "127.0.0.1", "port": SERVER.default_port, **kwargs})


# -- options ------------------------------------------------------------------


def test_defaults() -> None:
    options = http_options(SERVER, environ={})
    assert options == HttpOptions(host="127.0.0.1", port=8799, path="/mcp")
    assert http_options(OTHER, environ={}).port == 8798
    assert options.public_host is None
    assert options.rate_limit_per_minute == 60


def test_rate_limit_from_environment() -> None:
    var = "SBF_RATE_LIMIT_PER_MINUTE"
    assert http_options(SERVER, environ={var: "0"}).rate_limit_per_minute == 0
    assert http_options(SERVER, environ={var: "5"}).rate_limit_per_minute == 5
    assert http_options(OTHER, environ={var: "5"}).rate_limit_per_minute == 60  # not its prefix
    with pytest.raises(ServeConfigError, match=var):
        http_options(SERVER, environ={var: "lots"})
    with pytest.raises(ServeConfigError, match=var):
        validate(SERVER, opts(no_auth=True, rate_limit_per_minute=-1), auth_configured=False)


def test_environment_fills_in_and_flags_win() -> None:
    env = {
        "SBF_HOST": "::1",
        "SBF_PORT": "9001",
        "SBF_HTTP_PATH": "/env",
        "SBF_PUBLIC_URL": "https://notes.example.com",
    }
    from_env = http_options(SERVER, environ=env)
    assert from_env == HttpOptions(
        host="::1", port=9001, path="/env", public_url="https://notes.example.com"
    )
    assert from_env.public_host == "notes.example.com"
    from_flags = http_options(SERVER, host="localhost", port=9002, path="/flag", environ=env)
    assert (from_flags.host, from_flags.port, from_flags.path) == ("localhost", 9002, "/flag")


def test_port_zero_flag_is_kept_not_replaced_by_default() -> None:
    assert http_options(SERVER, port=0, environ={}).port == 0


def test_empty_environment_values_mean_unset() -> None:
    assert http_options(SERVER, environ={"SBF_PORT": "", "SBF_PUBLIC_URL": ""}) == opts()


def test_non_numeric_port_in_environment() -> None:
    with pytest.raises(ServeConfigError, match="SBF_PORT"):
        http_options(SERVER, environ={"SBF_PORT": "eighty"})


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("::", False),
        ("10.42.20.11", False),
        ("mercury", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


# -- guardrails ---------------------------------------------------------------


def test_http_without_sign_in_needs_no_auth() -> None:
    with pytest.raises(ServeConfigError, match="--no-auth"):
        validate(SERVER, opts(), auth_configured=False)


def test_no_auth_on_loopback_is_allowed() -> None:
    validate(SERVER, opts(no_auth=True), auth_configured=False)
    validate(SERVER, opts(host="::1", no_auth=True), auth_configured=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.42.20.11", "::"])
def test_no_auth_off_loopback_is_refused(host: str) -> None:
    with pytest.raises(ServeConfigError, match="loopback"):
        validate(SERVER, opts(host=host, no_auth=True), auth_configured=False)


def test_no_auth_with_public_url_is_refused() -> None:
    options = opts(no_auth=True, public_url="http://127.0.0.1:8775")
    with pytest.raises(ServeConfigError, match="public URL"):
        validate(SERVER, options, auth_configured=False)


def test_no_auth_with_sign_in_configured_is_refused() -> None:
    with pytest.raises(ServeConfigError, match="drop one"):
        validate(SERVER, opts(no_auth=True), auth_configured=True)


def test_sign_in_allows_any_host() -> None:
    options = opts(host="0.0.0.0", public_url="https://notes.example.com")
    validate(SERVER, options, auth_configured=True)


@pytest.mark.parametrize(
    "url",
    [
        "notes.example.com",  # no scheme
        "ftp://notes.example.com",
        "https://",
        "https://notes.example.com/mcp",  # a path: the origin only
        "https://notes.example.com/?x=1",
        "http://notes.example.com",  # plain http off loopback
    ],
)
def test_bad_public_urls(url: str) -> None:
    with pytest.raises(ServeConfigError, match="public URL"):
        validate(SERVER, opts(public_url=url), auth_configured=True)


def test_good_public_urls() -> None:
    for url in ("https://notes.example.com", "https://notes.example.com/", "http://127.0.0.1:9"):
        validate(SERVER, opts(public_url=url), auth_configured=True)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_port_out_of_range(port: int) -> None:
    with pytest.raises(ServeConfigError, match="port"):
        validate(SERVER, opts(port=port, no_auth=True), auth_configured=False)


def test_path_must_start_with_slash() -> None:
    with pytest.raises(ServeConfigError, match="path"):
        validate(SERVER, opts(path="mcp", no_auth=True), auth_configured=False)


# -- the app ------------------------------------------------------------------


def test_app_kwargs_are_stateless_json_with_strict_hosts() -> None:
    kwargs = http_app_kwargs(opts(public_url="https://notes.example.com"))
    assert kwargs == {
        "path": "/mcp",
        "stateless_http": True,
        "json_response": True,
        "host_origin_protection": True,
        "allowed_hosts": ["notes.example.com"],
    }
    assert http_app_kwargs(opts())["allowed_hosts"] == []


def test_run_http_passes_the_same_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    server = create()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(server, "run", lambda **kwargs: seen.update(kwargs))
    options = opts(host="::1", port=9400, no_auth=True)
    run_http(server, options)
    assert seen == {
        "transport": "http",
        "host": "::1",
        "port": 9400,
        "show_banner": False,
        "uvicorn_config": {"access_log": False},
        **http_app_kwargs(options),
    }


@asynccontextmanager
async def serving(options: HttpOptions, server: FastMCP | None = None) -> AsyncIterator[ASGIServer]:
    kwargs = http_app_kwargs(options)
    path = kwargs.pop("path")
    async with asgi_server(server or create(), path=path, **kwargs) as running:
        yield running


def raw_client(running: ASGIServer, host: str | None = None) -> httpx2.AsyncClient:
    """A plain HTTP client into the app, connected as uvicorn would be: on 127.0.0.1.

    ``host`` changes only the Host header. (The ASGI ``server`` address comes from
    the base URL, and the guard always trusts the address it is bound to, so a test
    that put another name there would be testing nothing.)
    """
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=running.app),
        base_url="http://127.0.0.1",
        headers={"Host": host} if host else None,
    )


async def test_round_trip_over_http() -> None:
    async with serving(opts(no_auth=True)) as running, running.client() as client:
        names = {tool.name for tool in await client.list_tools()}
        result = await client.call_tool("fake_echo", {"text": "hello"})
    assert "fake_echo" in names
    assert result.is_error is False


async def test_stateless_requests_need_no_session() -> None:
    """Two separate clients, no shared session: each request stands alone."""
    async with serving(opts(no_auth=True)) as running:
        for _ in range(2):
            async with running.client() as client:
                assert await client.list_tools()


# -- /healthz -----------------------------------------------------------------


def with_healthz(probe: Any, key: str = "store", cache_seconds: float = 0.0) -> FastMCP:
    server = create()
    server.custom_route("/healthz", methods=["GET"])(
        make_healthz(probe, key, cache_seconds=cache_seconds)
    )
    return server


async def health(probe: Any, **kwargs: Any) -> tuple[int, Any]:
    async with serving(opts(no_auth=True), with_healthz(probe, **kwargs)) as running:
        async with raw_client(running) as http:
            response = await http.get("/healthz")
    return response.status_code, response.json()


async def test_healthz_up() -> None:
    async def up() -> bool:
        return True

    assert await health(up, key="joplin") == (200, {"server": True, "joplin": True})


async def test_healthz_down() -> None:
    async def down() -> bool:
        return False

    assert await health(down) == (503, {"server": True, "store": False})


async def test_healthz_a_probe_that_raises_is_down() -> None:
    async def broken() -> bool:
        raise RuntimeError("the probe's own text must not be shown")

    assert await health(broken) == (503, {"server": True, "store": False})


async def test_healthz_asks_every_time_without_a_cache() -> None:
    asked: list[int] = []

    async def probe() -> bool:
        asked.append(1)
        return len(asked) > 1

    async with serving(opts(no_auth=True), with_healthz(probe)) as running:
        async with raw_client(running) as http:
            assert (await http.get("/healthz")).status_code == 503
            assert (await http.get("/healthz")).status_code == 200
    assert len(asked) == 2


async def test_healthz_reuses_an_answer_while_the_cache_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr("second_brain_core.health.time.monotonic", lambda: now[0])
    asked: list[int] = []

    async def probe() -> bool:
        asked.append(1)
        return True

    async with serving(opts(no_auth=True), with_healthz(probe, cache_seconds=10)) as running:
        async with raw_client(running) as http:
            for _ in range(3):
                assert (await http.get("/healthz")).status_code == 200
            assert len(asked) == 1
            now[0] += 10.5
            assert (await http.get("/healthz")).status_code == 200
    assert len(asked) == 2


async def test_unknown_host_header_is_refused() -> None:
    async def up() -> bool:
        return True

    options = opts(public_url="https://notes.example.com")
    async with serving(options, with_healthz(up)) as running:
        async with raw_client(running, "evil.example") as http:
            assert (await http.get("/healthz")).status_code == 421
        async with raw_client(running, "notes.example.com") as http:
            assert (await http.get("/healthz")).status_code == 200


# -- the transport ------------------------------------------------------------


def test_stdio_is_the_default_and_fastmcps_own_setting_is_ignored() -> None:
    assert choose_transport(SERVER, None, environ={}) == "stdio"
    assert choose_transport(SERVER, None, environ={"FASTMCP_TRANSPORT": "http"}) == "stdio"


def test_the_flag_wins_over_the_servers_variable() -> None:
    assert choose_transport(SERVER, None, environ={"SBF_TRANSPORT": "http"}) == "http"
    assert choose_transport(SERVER, "stdio", environ={"SBF_TRANSPORT": "http"}) == "stdio"
    assert choose_transport(OTHER, None, environ={"SBF_TRANSPORT": "http"}) == "stdio"


def test_an_unknown_transport_is_refused() -> None:
    with pytest.raises(ServeConfigError, match="SBF_TRANSPORT must be one of stdio, http"):
        choose_transport(SERVER, None, environ={"SBF_TRANSPORT": "sse"})


def test_http_flags_are_refused_on_stdio() -> None:
    refuse_http_flags({"--host": None, "--no-auth": None})
    with pytest.raises(ServeConfigError, match="--host, --no-auth only apply to --transport http"):
        refuse_http_flags({"--host": "0.0.0.0", "--port": None, "--no-auth": True})


def test_run_stdio_names_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASTMCP_TRANSPORT", "http")
    server = create()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(server, "run", lambda **kwargs: seen.update(kwargs))
    run_stdio(server)
    assert seen == {"transport": "stdio"}


# -- the middleware -----------------------------------------------------------


def test_the_middleware_stack_in_order() -> None:
    stack = build_middleware(
        SERVER, allowed_ids=frozenset({"1"}), rate_limit_per_minute=60, signed_in=True
    )
    assert [type(m) for m in stack] == [ToolCallRateLimit, AuthMiddleware, AuditMiddleware]
    bare = build_middleware(SERVER, allowed_ids=None, rate_limit_per_minute=0, signed_in=False)
    assert [type(m) for m in bare] == [AuditMiddleware]


def test_sign_in_without_ids_is_refused() -> None:
    with pytest.raises(ValueError, match="allowed GitHub ids"):
        build_middleware(SERVER, allowed_ids=None, rate_limit_per_minute=0, signed_in=True)
