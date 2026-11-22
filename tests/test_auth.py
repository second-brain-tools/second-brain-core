"""Tests for GitHub sign-in: settings, the id allowlist, and the OAuth proxy over HTTP.

GitHub is faked with an ``httpx2.MockTransport`` handed to the verifier, so these
run offline. They run on the fake server (``tests/fakeserver``), and the settings
checks run under two prefixes, so nothing is tied to one server's names.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from fakeserver import OTHER, SERVER, create
from fastmcp import Client
from fastmcp.server.auth import AccessToken, StaticTokenVerifier, TokenVerificationError
from fastmcp.utilities.tests import ASGIServer, asgi_server
from key_value.aio.stores.memory import MemoryStore

from second_brain_core.auth import (
    CLAUDE_CALLBACK,
    AllowlistCheck,
    AllowlistGitHubTokenVerifier,
    AuthSettings,
    build_auth,
    is_allowed,
    parse_github_ids,
)
from second_brain_core.errors import ServeConfigError
from second_brain_core.remote import HttpOptions, http_app_kwargs, validate
from second_brain_core.server import Server

JOHN = "65598"
SIGNING_KEY = "k" * 40
PUBLIC_URL = "https://notes.example.com"


def full_env(server: Server = SERVER) -> dict[str, str]:
    return {
        server.var("GITHUB_CLIENT_ID"): "Ov23liClientId",
        server.var("GITHUB_CLIENT_SECRET"): "client-secret-value",
        server.var("JWT_SIGNING_KEY"): SIGNING_KEY,
        server.var("ALLOWED_GITHUB_IDS"): JOHN,
    }


FULL_ENV = full_env()
BOTH = pytest.mark.parametrize("server", [SERVER, OTHER], ids=lambda s: s.prefix)

# -- settings -----------------------------------------------------------------


@BOTH
def test_no_sign_in_settings_means_none(server: Server) -> None:
    assert AuthSettings.from_env(server, {}) is None
    assert AuthSettings.from_env(server, {name: " " for name in full_env(server)}) is None


@BOTH
def test_full_settings(server: Server) -> None:
    settings = AuthSettings.from_env(server, full_env(server))
    assert settings is not None
    assert settings.allowed_ids == frozenset({JOHN})
    assert settings.redirect_uris == (CLAUDE_CALLBACK,)


def test_another_servers_variables_are_not_read() -> None:
    assert AuthSettings.from_env(OTHER, FULL_ENV) is None


@pytest.mark.parametrize("missing", sorted(FULL_ENV))
def test_partial_settings_name_what_is_missing_without_values(missing: str) -> None:
    env = {k: v for k, v in FULL_ENV.items() if k != missing}
    with pytest.raises(ServeConfigError) as exc:
        AuthSettings.from_env(SERVER, env)
    message = str(exc.value)
    assert missing in message
    for value in env.values():
        assert value not in message


def test_short_signing_key_is_refused() -> None:
    with pytest.raises(ServeConfigError, match="at least 32"):
        AuthSettings.from_env(SERVER, {**FULL_ENV, "SBF_JWT_SIGNING_KEY": "short"})


def test_repr_never_shows_secrets() -> None:
    settings = AuthSettings.from_env(SERVER, FULL_ENV)
    text = repr(settings) + str(settings)
    for secret in ("client-secret-value", SIGNING_KEY, "Ov23liClientId"):
        assert secret not in text


def test_redirect_override_and_its_check() -> None:
    env = {**FULL_ENV, "SBF_OAUTH_REDIRECT_URIS": f"{CLAUDE_CALLBACK}, https://claude.com/cb"}
    settings = AuthSettings.from_env(SERVER, env)
    assert settings is not None
    assert settings.redirect_uris == (CLAUDE_CALLBACK, "https://claude.com/cb")
    with pytest.raises(ServeConfigError, match="SBF_OAUTH_REDIRECT_URIS must be https"):
        AuthSettings.from_env(
            SERVER, {**FULL_ENV, "SBF_OAUTH_REDIRECT_URIS": "http://localhost:3000/cb"}
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("65598", {"65598"}), ("65598, 1 ,2", {"65598", "1", "2"}), ("007", {"7"})],
)
def test_parse_ids(raw: str, expected: set[str]) -> None:
    assert parse_github_ids(raw) == frozenset(expected)


@pytest.mark.parametrize("raw", ["jomkz", "65598,jomkz", "-1", "6.5"])
def test_ids_must_be_numeric(raw: str) -> None:
    with pytest.raises(ServeConfigError, match="numeric"):
        parse_github_ids(raw)
    with pytest.raises(ServeConfigError, match="SBO_ALLOWED_GITHUB_IDS takes numeric"):
        AuthSettings.from_env(OTHER, {**full_env(OTHER), "SBO_ALLOWED_GITHUB_IDS": raw})


def test_ids_must_not_be_empty() -> None:
    with pytest.raises(ServeConfigError, match="no ids"):
        parse_github_ids(" , ")


def test_sign_in_needs_a_public_url() -> None:
    with pytest.raises(ServeConfigError, match="SBF_PUBLIC_URL"):
        validate(SERVER, HttpOptions(host="127.0.0.1", port=8799), auth_configured=True)


# -- the allowlist ------------------------------------------------------------


def fake_github(
    user: dict[str, Any] | None = None, *, status: int = 200, scopes: str = "read:user"
) -> httpx2.AsyncClient:
    """An httpx2 client whose GitHub answers with ``user`` for any token."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/user":
            return httpx2.Response(status, json=user or {})
        if request.url.path == "/user/repos":
            return httpx2.Response(200, json=[], headers={"X-OAuth-Scopes": scopes})
        return httpx2.Response(404)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def verifier(github: httpx2.AsyncClient, allowed: str = JOHN) -> AllowlistGitHubTokenVerifier:
    return AllowlistGitHubTokenVerifier(
        server=SERVER,
        allowed_ids=frozenset({allowed}),
        required_scopes=["read:user"],
        http_client=github,
    )


async def test_listed_id_passes() -> None:
    token = await verifier(fake_github({"id": 65598, "login": "jomkz"})).verify_token("t")
    assert token is not None
    assert token.claims["sub"] == JOHN


async def test_other_id_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    assert await verifier(fake_github({"id": 1, "login": "someone"})).verify_token("t") is None
    (line,) = [r for r in caplog.records if r.name == "fakeserver.auth"]
    assert (
        line.getMessage() == "refused GitHub account someone (id 1): not in SBF_ALLOWED_GITHUB_IDS"
    )


async def test_right_login_wrong_id_is_refused() -> None:
    """A renamed account's login re-registered by someone else must not get in."""
    assert await verifier(fake_github({"id": 777, "login": "jomkz"})).verify_token("t") is None


async def test_user_without_an_id_is_never_accepted() -> None:
    with pytest.raises(TokenVerificationError):
        await verifier(fake_github({"login": "jomkz"})).verify_token("t")


async def test_github_rejecting_the_token_is_refused() -> None:
    assert await verifier(fake_github(status=401)).verify_token("t") is None


async def test_github_down_is_a_temporary_failure_not_a_pass() -> None:
    with pytest.raises(TokenVerificationError):
        await verifier(fake_github(status=503)).verify_token("t")


async def test_missing_scope_is_refused() -> None:
    github = fake_github({"id": 65598, "login": "jomkz"}, scopes="gist")
    assert await verifier(github).verify_token("t") is None


def test_verifier_needs_ids() -> None:
    with pytest.raises(ValueError, match="allowed_ids"):
        AllowlistGitHubTokenVerifier(server=SERVER, allowed_ids=frozenset())


def _token(claims: dict[str, Any]) -> AccessToken:
    return AccessToken(token="t", client_id="c", scopes=[], claims=claims)


@pytest.mark.parametrize(
    ("token", "allowed"),
    [
        (_token({"sub": JOHN}), True),
        (_token({"sub": "1"}), False),
        (_token({"sub": 65598}), False),  # not the string form the verifier sets
        (_token({}), False),
        (None, False),
    ],
)
def test_is_allowed(token: AccessToken | None, allowed: bool) -> None:
    assert is_allowed(token, frozenset({JOHN})) is allowed


def test_server_with_sign_in_needs_ids() -> None:
    with pytest.raises(ValueError, match="allowed GitHub ids"):
        create(auth=StaticTokenVerifier(tokens={}))


# -- over HTTP ----------------------------------------------------------------


@asynccontextmanager
async def serving(server_factory: Callable[[], Any]) -> AsyncIterator[ASGIServer]:
    kwargs = http_app_kwargs(HttpOptions(host="127.0.0.1", port=8799, public_url=PUBLIC_URL))
    path = kwargs.pop("path")
    async with asgi_server(server_factory(), path=path, **kwargs) as running:
        yield running


def raw(running: ASGIServer) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=running.app), base_url="http://127.0.0.1"
    )


async def test_allowlisted_verifier_over_http() -> None:
    """The verifier as the server's auth: John's token works, anyone else's is 401."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        user = {"john": {"id": 65598, "login": "jomkz"}, "other": {"id": 2, "login": "x"}}
        if request.url.path == "/user":
            return httpx2.Response(200, json=user[token])
        return httpx2.Response(200, json=[], headers={"X-OAuth-Scopes": "read:user"})

    github = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    ids = frozenset({JOHN})

    async with serving(lambda: create(auth=verifier(github), allowed_ids=ids)) as running:
        async with running.client(auth="john") as client:
            assert {t.name for t in await client.list_tools()}
        async with raw(running) as http:
            for headers in ({}, {"Authorization": "Bearer other"}):
                response = await http.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream", **headers},
                )
                assert response.status_code == 401
                assert "Bearer" in response.headers["www-authenticate"]


async def test_middleware_hides_tools_from_unlisted_tokens() -> None:
    """Second layer: even a token the verifier accepted shows no tools unless listed."""
    tokens = {
        "john": {"client_id": "c", "scopes": [], "sub": JOHN},
        "other": {"client_id": "c", "scopes": [], "sub": "2"},
    }
    ids = frozenset({JOHN})

    def factory() -> Any:
        return create(auth=StaticTokenVerifier(tokens=tokens), allowed_ids=ids)

    async with serving(factory) as running:
        async with running.client(auth="john") as client:
            assert await client.list_tools()
        async with running.client(auth="other") as client:
            assert await client.list_tools() == []
            result = await client.call_tool("fake_echo", {"text": "x"}, raise_on_error=False)
            assert result.is_error


def test_allowlist_check_reads_the_token() -> None:
    check = AllowlistCheck(frozenset({JOHN}))
    ctx_ok: Any = type("Ctx", (), {"token": _token({"sub": JOHN})})()
    ctx_no: Any = type("Ctx", (), {"token": None})()
    assert check(ctx_ok) is True
    assert check(ctx_no) is False


def oauth_server() -> Any:
    settings = AuthSettings.from_env(SERVER, FULL_ENV)
    assert settings is not None
    provider = build_auth(SERVER, settings, PUBLIC_URL, client_storage=MemoryStore())
    return create(auth=provider, allowed_ids=settings.allowed_ids)


async def test_oauth_metadata_advertises_read_user() -> None:
    async with serving(oauth_server) as running, raw(running) as http:
        response = await http.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"].rstrip("/") == PUBLIC_URL
    assert metadata["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"
    assert metadata["scopes_supported"] == ["read:user"]
    assert "S256" in metadata["code_challenge_methods_supported"]


@pytest.mark.parametrize(
    ("redirect", "ok"),
    [
        (CLAUDE_CALLBACK, True),
        ("https://evil.example/callback", False),
        ("http://localhost:3000/callback", False),
    ],
)
async def test_only_claude_may_register(redirect: str, ok: bool) -> None:
    body = {
        "redirect_uris": [redirect],
        "client_name": "test",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with serving(oauth_server) as running, raw(running) as http:
        response = await http.post("/register", json=body)
    assert (response.status_code == 201) is ok, response.text


async def test_mcp_without_a_token_is_401_with_discovery() -> None:
    async with serving(oauth_server) as running, raw(running) as http:
        response = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


async def test_in_memory_client_of_a_signed_in_server_sees_nothing() -> None:
    """Fails closed: with no HTTP request there is no token, so the allowlist hides all.

    (stdio users run a separate, sign-in-free server; this guards against a signed-in
    server ever being exposed through a transport that skips HTTP auth.)
    """
    async with Client(oauth_server()) as client:
        assert await client.list_tools() == []


# -- the token names its client (#92) -------------------------------------------


async def signed_in_through_the_proxy(
    provider: Any, client_name: str | None, client_id: str = "reg-desktop"
) -> str:
    """A FastMCP token from ``provider``, as Claude gets one at the end of sign-in.

    The browser half (consent, GitHub's page, the callback) is skipped: the code the
    callback would have stored is put in the proxy's code store directly, then
    exchanged the way the token endpoint does.
    """
    import time

    from fastmcp.server.auth.oauth_proxy.models import ClientCode
    from mcp.server.auth.provider import AuthorizationCode
    from mcp.shared.auth import OAuthClientInformationFull

    client = OAuthClientInformationFull(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=[CLAUDE_CALLBACK],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="none",
    )
    await provider.register_client(client)
    registered = await provider.get_client(client_id)
    now = time.time()
    await provider._code_store.put(
        key="the-code",
        value=ClientCode(
            code="the-code",
            client_id=client_id,
            redirect_uri=CLAUDE_CALLBACK,
            code_challenge=None,
            code_challenge_method="S256",
            scopes=["read:user"],
            idp_tokens={"access_token": "gho_upstream", "token_type": "bearer"},
            expires_at=now + 60,
            created_at=now,
        ),
    )
    code = AuthorizationCode(
        code="the-code",
        scopes=["read:user"],
        expires_at=now + 60,
        client_id=client_id,
        code_challenge="x",
        redirect_uri=CLAUDE_CALLBACK,  # type: ignore[arg-type]
        redirect_uri_provided_explicitly=True,
    )
    tokens = await provider.exchange_authorization_code(registered, code)
    return str(tokens.access_token)


async def test_a_proxy_token_names_the_client_it_was_issued_to(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The proxy's own token says which client it was issued to; the log uses it.

    FastMCP's proxy returns the token its upstream verifier built, whose client id
    is the GitHub user's, so the registered name was never found.
    """
    caplog.set_level("INFO", logger="fakeserver")
    settings = AuthSettings.from_env(SERVER, FULL_ENV)
    assert settings is not None
    provider = build_auth(
        SERVER,
        settings,
        PUBLIC_URL,
        client_storage=MemoryStore(),
        github_http_client=fake_github({"id": int(JOHN), "login": "jomkz"}),
    )
    server = create(auth=provider, allowed_ids=settings.allowed_ids, clients=provider)
    async with serving(lambda: server) as running:
        token = await signed_in_through_the_proxy(provider, "Claude Desktop")
        loaded = await provider.load_access_token(token)
        assert isinstance(loaded, AccessToken)
        assert loaded.client_id == "reg-desktop"
        assert loaded.claims["sub"] == JOHN  # the caller, and the allow-list, unchanged
        async with running.client(auth=token) as client:
            await client.call_tool("fake_echo", {"text": "x"})
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tool=")]
    assert f" caller={JOHN} client=Claude_Desktop " in line


@pytest.mark.parametrize("second_look", ["expired since", "no client id", "same id"])
async def test_a_token_that_names_no_other_client_keeps_the_upstream_id(
    second_look: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No client id, or the same one: the token as FastMCP gives it. Expired since: refused."""
    settings = AuthSettings.from_env(SERVER, FULL_ENV)
    assert settings is not None
    provider = build_auth(
        SERVER,
        settings,
        PUBLIC_URL,
        client_storage=MemoryStore(),
        github_http_client=fake_github({"id": int(JOHN), "login": "jomkz"}),
    )
    server = create(auth=provider, allowed_ids=settings.allowed_ids, clients=provider)
    async with serving(lambda: server):
        token = await signed_in_through_the_proxy(provider, None)
        assert await provider.load_access_token("not-a-token") is None
        issuer = provider.jwt_issuer
        real = issuer.verify_token
        looks: list[int] = []

        def verify(raw: str) -> dict[str, Any]:
            looks.append(1)
            if len(looks) == 1:  # the proxy's own check, as it is
                return real(raw)
            if second_look == "expired since":
                raise ValueError("expired a moment ago")
            if second_look == "no client id":
                return {}
            return {"client_id": JOHN}

        monkeypatch.setattr(issuer, "verify_token", verify)
        loaded = await provider.load_access_token(token)
    if second_look == "expired since":
        # Refused (401), as an expired token is, so the client refreshes it.
        assert loaded is None
        return
    assert isinstance(loaded, AccessToken)
    assert loaded.client_id == JOHN  # the upstream verifier's, as FastMCP gives it
    assert loaded.claims["sub"] == JOHN
