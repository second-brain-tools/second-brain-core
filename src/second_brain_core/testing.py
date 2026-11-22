"""Security regression checks each server runs against itself, in its own CI.

They guard rules that hold only if every server keeps them: they live here so the
rule and its test are written once, and each server's suite calls them on its own
package (see ``tests/test_core_checks.py`` in each server). Each raises
``AssertionError`` with the reason when the rule is broken. No pytest needed.
"""

import os
import subprocess
import sys
import unicodedata
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


def invisible_characters(
    root: Path, patterns: Iterable[str] = ("src/**/*.py", "tests/**/*.py")
) -> list[str]:
    """Where ``root``'s files hold a character a reader can't see: ``path:line U+XXXX``.

    Format, control, private-use, surrogate and unassigned characters (a tab
    aside), line and paragraph separators, variation selectors, and every space
    but the plain one. "Unassigned" follows the running Python's Unicode tables,
    so a character a newer Unicode assigns stops counting on a newer Python.
    """
    offenders = []
    paths = sorted({path for pattern in patterns for path in root.glob(pattern)})
    for path in paths:
        # Split on newlines only: splitlines() would also split on U+2028 and its kind,
        # which are among the characters looked for. A Windows checkout's \r goes too.
        lines = path.read_text(encoding="utf-8").split("\n")
        for number, line in enumerate(lines, start=1):
            for char in line.removesuffix("\r"):
                category = unicodedata.category(char)
                hidden = category in ("Cf", "Cc", "Co", "Cs", "Cn", "Zl", "Zp")
                selector = 0x180B <= ord(char) <= 0x180F or 0xFE00 <= ord(char) <= 0xFE0F
                odd_space = category == "Zs" and char != " "
                if (hidden and char != "\t") or selector or odd_space:
                    offenders.append(
                        f"{path.relative_to(root).as_posix()}:{number} U+{ord(char):04X}"
                    )
    return offenders


def check_no_invisible_characters(
    root: Path, patterns: Iterable[str] = ("src/**/*.py", "tests/**/*.py")
) -> None:
    """No source or test file under ``root`` holds an invisible or bidirectional character.

    A reviewer must be able to see every character of the code. An editor or a
    tool that turns an escape into the character itself would otherwise leave a
    zero-width space or a direction override sitting unseen in a source file, so
    such characters are written as escapes. ``root`` is the project's checkout.
    """
    offenders = invisible_characters(root, patterns)
    assert offenders == [], f"invisible characters in source files: {offenders}"
