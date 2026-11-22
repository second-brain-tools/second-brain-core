#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Install second-brain-core the way a server does, in a fresh environment, and use it.

    uv run scripts/smoke.py                                   # the latest release on PyPI
    uv run scripts/smoke.py --from "second-brain-core==0.1.0"
    uv run scripts/smoke.py --from dist/second_brain_core-0.1.0-py3-none-any.whl
    uv run scripts/smoke.py --python 3.13                     # on a chosen Python
    uv run scripts/smoke.py --refresh --from "second-brain-core==0.1.1"   # just released

It installs the package with its dependencies resolved fresh from PyPI, as a
server's install does, then in that environment imports every module, checks that
importing the package pins FastMCP's env file, and builds a small server on the
shared pieces and calls a tool on it. CI runs it on Linux, macOS and Windows
against the wheel it just built.
"""

import argparse
import subprocess
import sys
from collections.abc import Sequence

# Run inside the fresh environment. Prints one "ok ..." line per check, and exits
# non-zero at the first that fails.
PROBE = r"""
import importlib, os, sys
os.environ.pop("FASTMCP_ENV_FILE", None)
import second_brain_core
assert os.environ["FASTMCP_ENV_FILE"] == os.devnull, "the package did not pin FASTMCP_ENV_FILE"
print("ok    importing the package pins FastMCP's env file")
names = ["annotations", "audit", "auth", "env", "errors", "failure", "health", "recent",
         "remote", "server", "testing", "textclean", "semantic.chunking", "semantic.engine",
         "semantic.embedders", "semantic.stores", "semantic.sync", "semantic.testing",
         "semantic.types"]
for name in names:
    importlib.import_module(f"second_brain_core.{name}")
print(f"ok    every module imports: {len(names)}")

import asyncio
from fastmcp import Client, FastMCP
from second_brain_core.remote import build_middleware
from second_brain_core.server import Server

server = Server(
    name="smoke-server", prefix="SMOKE_", package="second_brain_core", default_port=8790
)
app = FastMCP(server.name, middleware=build_middleware(
    server, allowed_ids=None, rate_limit_per_minute=60, signed_in=False), mask_error_details=True)

@app.tool
def smoke_echo(text: str) -> str:
    return text

async def call() -> str:
    async with Client(app) as client:
        result = await client.call_tool("smoke_echo", {"text": "hello"})
        return result.content[0].text

assert asyncio.run(call()) == "hello"
print("ok    a server built on the shared middleware answers a tool call")

from second_brain_core.errors import SemanticUnavailableError
from second_brain_core.semantic.embedders import build_embedder
try:
    build_embedder(server, "fastembed")
except SemanticUnavailableError as exc:
    assert "smoke-server[semantic]" in str(exc), str(exc)
    print("ok    without the semantic extra, building an embedder says what to install")
else:
    print("info  the semantic extra is installed")
print(f"info  second-brain-core {second_brain_core.__version__} on Python {sys.version.split()[0]}")
"""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--from",
        dest="spec",
        default="second-brain-core",
        help="what to install: a requirement or a wheel (default: the latest on PyPI)",
    )
    parser.add_argument("--python", help="the Python to run on, e.g. 3.13")
    parser.add_argument(
        "--refresh", action="store_true", help="ignore uv's cache (for a version just released)"
    )
    args = parser.parse_args(argv)
    command = ["uv", "run", "--isolated", "--no-project", "--with", args.spec]
    if args.python:
        command += ["--python", args.python]
    if args.refresh:
        command.append("--refresh")
    command += ["python", "-c", PROBE]
    done = subprocess.run(command)
    print("all passed" if done.returncode == 0 else "FAILED", flush=True)
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
