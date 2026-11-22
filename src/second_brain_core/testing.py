"""Security regression checks each server runs against itself, in its own CI.

They guard rules that hold only if every server keeps them: they live here so the
rule and its test are written once, and each server's suite calls them on its own
package (see ``tests/test_core_checks.py`` in each server). Each raises
``AssertionError`` with the reason when the rule is broken. No pytest needed.
"""

import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


def fastmcp_transport(package: str, cwd: Path, **extra_env: str) -> str:
    """FastMCP's transport setting in a fresh interpreter that imports ``package`` in ``cwd``.

    No ``FASTMCP_*`` variable is set unless ``extra_env`` sets one.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("FASTMCP_")} | extra_env
    code = f"import {package}, fastmcp; print(fastmcp.settings.transport)"
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def check_dotenv_cannot_set_fastmcp(package: str, workdir: Path) -> None:
    """A ``.env`` in the working directory can't set FastMCP's settings.

    FastMCP reads ``FASTMCP_*`` from a ``.env`` in the working directory, the
    transport among them, and an MCP server's working directory is whatever project
    the client has open. Importing the server must stop that read (the package
    imports ``second_brain_core`` before fastmcp). ``workdir`` must be an empty
    directory; a ``.env`` is written into it.
    """
    (workdir / ".env").write_text("FASTMCP_TRANSPORT=http\nFASTMCP_HOST=0.0.0.0\n")
    got = fastmcp_transport(package, workdir)
    assert got == "stdio", f"a working directory's .env set FastMCP's transport to {got!r}"
    # The control: the same file does switch it once the operator names it, so the
    # check above passes because the file went unread, not because it was harmless.
    named = fastmcp_transport(package, workdir, FASTMCP_ENV_FILE=".env")
    assert named == "http", f"the control failed: a named env file gave {named!r}"


def check_each_imports_alone(modules: Iterable[str]) -> None:
    """Each of ``modules`` imports in a fresh interpreter, first of all.

    An import cycle shows only when the module that starts it is imported first,
    which a test suite (importing everything) never does but an entry point does.
    """
    for module in modules:
        done = subprocess.run(
            [sys.executable, "-c", f"import {module}"], capture_output=True, text=True
        )
        assert done.returncode == 0, f"import {module} failed on its own:\n{done.stderr}"
