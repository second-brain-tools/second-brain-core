"""A short memory of what a tool that writes answered, so a repeat writes nothing new.

A write is made by one request, and requests get repeated: a client that timed
out sends the call again, a model that saw an error tries once more. Without a
memory, each repeat is a second write. So a tool that writes remembers, for a few
minutes, what it answered for a request (by the request's digest), and gives that
answer to the repeat.

Kept in memory only and never written anywhere: it should hold digests and ids,
not content, and it is fine to lose on a restart.
"""

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")

REMEMBER_SECONDS = 600.0
REMEMBER_AT_MOST = 256


class Recent(Generic[T]):
    """What was answered for a request, for the last few minutes, by the request's digest."""

    def __init__(
        self,
        seconds: float = REMEMBER_SECONDS,
        at_most: int = REMEMBER_AT_MOST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._at_most = at_most
        self._clock = clock
        self._sent: OrderedDict[str, tuple[T, float]] = OrderedDict()

    def get(self, key: str) -> T | None:
        """What was remembered for this request, if recently enough."""
        entry = self._sent.get(key)
        if entry is None:
            return None
        value, at = entry
        if self._clock() - at > self._seconds:
            del self._sent[key]
            return None
        return value

    def put(self, key: str, value: T) -> None:
        """Remember ``value`` for this request, forgetting the oldest if full."""
        self._sent.pop(key, None)
        self._sent[key] = (value, self._clock())
        while len(self._sent) > self._at_most:
            self._sent.popitem(last=False)

    def forget(self, key: str) -> None:
        """Drop a request whose answer turned out not to stand."""
        self._sent.pop(key, None)


def request_key(*parts: str) -> str:
    """A digest of a request's parts, the same for the same request."""
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()
