"""Streamable HTTP mode for ``serve``: options, start-up guardrails, the middleware and the run.

stdio stays the default. HTTP mode exists so a Claude custom connector, which calls
MCP servers from Anthropic's cloud, can reach this server through a tunnel. The
server itself binds to loopback by default; the tunnel (or a reverse proxy) is what
publishes it, under the public URL that sign-in needs as its base URL.

HTTP mode is **stateless with plain JSON responses**: every request stands alone,
so a restart never breaks a client's session, and no idle event stream is held
open for a proxy to cut. None of the tools sends server-initiated messages, which
is the only thing a stateful session would add.
"""

import ipaddress
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from fastmcp.server.middleware import AuthMiddleware, Middleware

from .audit import AuditMiddleware, rate_limit
from .auth import allowlist_check
from .errors import ServeConfigError
from .server import Server

TRANSPORTS = ("stdio", "http")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PATH = "/mcp"
# Tool calls per minute per caller (a token bucket that allows bursts of half).
DEFAULT_RATE_LIMIT = 60


@dataclass(frozen=True)
class HttpOptions:
    """Where and how the HTTP transport listens."""

    host: str
    port: int
    path: str = DEFAULT_PATH
    public_url: str | None = None
    no_auth: bool = False
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT

    @property
    def public_host(self) -> str | None:
        """The host name of ``public_url`` (``notes.example.com``), if one is set."""
        if not self.public_url:
            return None
        return urlsplit(self.public_url).hostname


def is_loopback(host: str) -> bool:
    """Return True if ``host`` only ever answers on this machine."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def env_number(environ: Mapping[str, str], name: str) -> int | None:
    """A whole-number option from ``environ``; None when unset; refused when not a number.

    HTTP options are read once, at start-up, so a bad one is refused here.
    """
    raw = environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise ServeConfigError(f"{name} must be a number, got {raw!r}") from None


def http_options(
    server: Server,
    *,
    host: str | None = None,
    port: int | None = None,
    path: str | None = None,
    public_url: str | None = None,
    no_auth: bool = False,
    environ: Mapping[str, str] | None = None,
) -> HttpOptions:
    """Build :class:`HttpOptions` from command-line values, falling back to the server's variables.

    A flag wins over its environment variable, which wins over the default.
    ``--no-auth`` has no environment variable on purpose: switching sign-in off
    must be visible on the command line.
    """
    env = os.environ if environ is None else environ
    env_port = env_number(env, server.var("PORT"))
    env_rate = env_number(env, server.var("RATE_LIMIT_PER_MINUTE"))
    if port is None:
        port = env_port if env_port is not None else server.default_port
    return HttpOptions(
        host=host or env.get(server.var("HOST")) or DEFAULT_HOST,
        port=port,
        path=path or env.get(server.var("HTTP_PATH")) or DEFAULT_PATH,
        public_url=(public_url or env.get(server.var("PUBLIC_URL")) or None),
        no_auth=no_auth,
        rate_limit_per_minute=DEFAULT_RATE_LIMIT if env_rate is None else env_rate,
    )


def _check_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ServeConfigError(f"the public URL must be an http(s) URL with a host, got {url!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ServeConfigError(
            f"the public URL must be the server's origin only (https://host), got {url!r}"
        )
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        raise ServeConfigError(
            f"the public URL must use https unless it is a loopback address, got {url!r}"
        )


def validate(server: Server, options: HttpOptions, *, auth_configured: bool) -> None:
    """Refuse configurations that would expose a store without sign-in.

    Without sign-in, HTTP mode starts only with an explicit ``--no-auth``, only on a
    loopback address, and only without a public URL: that is a local test, nothing
    more. A tunnel reaches a loopback server just as well as a public one, which is
    why loopback alone is not enough to skip sign-in.
    """
    if not 1 <= options.port <= 65535:
        raise ServeConfigError(f"the port must be between 1 and 65535, got {options.port}")
    if options.rate_limit_per_minute < 0:
        raise ServeConfigError(f"{server.var('RATE_LIMIT_PER_MINUTE')} must be 0 (off) or more")
    if not options.path.startswith("/"):
        raise ServeConfigError(f"the endpoint path must start with '/', got {options.path!r}")
    if options.public_url:
        _check_public_url(options.public_url)
    if auth_configured:
        if options.no_auth:
            raise ServeConfigError("--no-auth was given but sign-in is configured; drop one")
        if not options.public_url:
            raise ServeConfigError(
                "sign-in needs the server's public URL "
                f"(--public-url or {server.var('PUBLIC_URL')}): "
                "GitHub and Claude are sent back to it"
            )
        return
    if not options.no_auth:
        raise ServeConfigError(
            "HTTP mode needs GitHub sign-in configured (docs/remote-access.md). "
            "For a local test only, add --no-auth."
        )
    if not is_loopback(options.host):
        raise ServeConfigError(
            f"--no-auth is for local testing only: bind to a loopback address, not {options.host}"
        )
    if options.public_url:
        raise ServeConfigError("--no-auth can't be combined with a public URL")


def http_app_kwargs(options: HttpOptions) -> dict[str, Any]:
    """The keyword arguments shared by ``FastMCP.run`` and ``FastMCP.http_app``.

    Kept in one place so the tests build exactly the app that ``serve`` runs.
    """
    allowed_hosts = [options.public_host] if options.public_host else []
    return {
        "path": options.path,
        "stateless_http": True,
        "json_response": True,
        # Strict Host/Origin checking: loopback names plus the public host only. A
        # request naming any other Host is answered 421 before it reaches MCP.
        "host_origin_protection": True,
        "allowed_hosts": allowed_hosts,
    }


def run_http(server: FastMCP, options: HttpOptions) -> None:
    """Serve ``server`` over streamable HTTP until interrupted."""
    server.run(
        transport="http",
        host=options.host,
        port=options.port,
        show_banner=False,
        # The access log would record the OAuth callback's query string, which
        # carries the authorization code. Tool calls are logged by audit.py.
        uvicorn_config={"access_log": False},
        **http_app_kwargs(options),
    )


def choose_transport(
    server: Server, flag: str | None, environ: Mapping[str, str] | None = None
) -> str:
    """The transport ``serve`` uses: the flag, else ``<prefix>TRANSPORT``, else stdio.

    Never FastMCP's own ``FASTMCP_TRANSPORT``: ``serve`` without an explicit HTTP
    transport must never listen on the network, where none of HTTP mode's checks
    (sign-in, loopback, the Host check) would have run. So a server runs stdio with
    ``server.run(transport="stdio")``, named.
    """
    env = os.environ if environ is None else environ
    name = server.var("TRANSPORT")
    transport = flag or env.get(name) or "stdio"
    if transport not in TRANSPORTS:
        raise ServeConfigError(f"{name} must be one of {', '.join(TRANSPORTS)}")
    return transport


def run_stdio(server: FastMCP) -> None:
    """Serve ``server`` over stdio: named, never left to ``FASTMCP_TRANSPORT``."""
    server.run(transport="stdio")


def refuse_http_flags(flags: Mapping[str, object]) -> None:
    """On stdio, refuse every HTTP-only flag that was given (``{"--host": value, ...}``).

    A flag counts as given when its value isn't None; pass ``True or None`` for a switch.
    """
    given = [flag for flag, value in flags.items() if value is not None]
    if given:
        raise ServeConfigError(f"{', '.join(given)} only apply to --transport http")


def build_middleware(
    server: Server,
    *,
    allowed_ids: frozenset[str] | None,
    rate_limit_per_minute: int,
    named_causes: Sequence[type[BaseException]] = (),
    signed_in: bool,
) -> list[Middleware]:
    """The middleware every server puts in front of its tools, in order.

    The per-caller rate limit (``rate_limit_per_minute``, 0 = off), the allow-list
    check as a second layer behind the token verifier, then the audit line. With
    sign-in (``signed_in``), ``allowed_ids`` is required.
    """
    if signed_in and not allowed_ids:
        raise ValueError("a server with sign-in needs its allowed GitHub ids")
    middleware: list[Middleware] = []
    if rate_limit_per_minute:
        middleware.append(rate_limit(rate_limit_per_minute))
    if allowed_ids:
        middleware.append(AuthMiddleware(auth=allowlist_check(allowed_ids)))
    middleware.append(AuditMiddleware(server, named_causes))
    return middleware
