"""The ``/healthz`` route both servers expose in HTTP mode."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

Probe = Callable[[], Awaitable[bool]]
Route = Callable[[Request], Awaitable[JSONResponse]]


def make_healthz(probe: Probe, key: str, *, cache_seconds: float = 0.0) -> Route:
    """A ``/healthz`` route: ``{"server": true, <key>: <probe's answer>}``.

    HTTP mode only (stdio has no routes). It is never behind sign-in, since FastMCP
    does not guard custom routes, so it reveals nothing about the store, no version
    and no configuration: two booleans. 503 when the probe says no, so a monitor
    sees it.

    ``probe`` answers whether the store behind the server answers. It must not
    raise for a store that is down; if it does anyway, the answer is no. With
    ``cache_seconds`` above 0 one answer is reused that long, for a probe that is
    costly (one that signs in, say); 0 asks every time.
    """
    last: dict[str, float | bool] = {}
    lock = asyncio.Lock()

    async def ask() -> bool:
        try:
            return bool(await probe())
        except Exception:
            return False

    async def healthz(_request: Request) -> JSONResponse:
        if cache_seconds <= 0:
            answer = await ask()
        else:
            async with lock:
                now = time.monotonic()
                if "at" not in last or now - float(last["at"]) > cache_seconds:
                    last["answer"] = await ask()
                    last["at"] = time.monotonic()
                answer = bool(last["answer"])
        return JSONResponse({"server": True, key: answer}, status_code=200 if answer else 503)

    return healthz
