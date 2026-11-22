"""Where a server's settings come from, and the readers that parse them.

**Where from.** The process environment first. Then either the env file named on the
command line (``--env-file``) or, failing that, the ``.env`` of the source checkout
the code runs from: never both. A named file is the whole configuration; a
checkout's ``.env`` would quietly fill in whatever the file leaves out. A variable
already set to something always wins over a file.

**How read.** The readers never raise. A value that can't be read gives ``None``
(or the reader's documented answer), the server keeps its default and records the
variable's name, and its commands refuse to start until the value is fixed. Raising
instead would break ``/healthz``, which reads the settings on every request.
"""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from .errors import ServeConfigError
from .server import Server

_YES = frozenset({"1", "true", "yes", "on"})
_NO = frozenset({"0", "false", "no", "off"})


def apply_env(values: Mapping[str, str | None]) -> None:
    """Put ``values`` into the environment, keeping any variable already set to something.

    A variable that is set to nothing doesn't count as set here. Otherwise an empty
    filter variable exported by a shell or a service manager would silently beat the
    filter written in the file.
    """
    for name, value in values.items():
        if value is not None and not os.environ.get(name):
            os.environ[name] = value


def load_env_file(path: Path) -> None:
    """Load ``KEY=value`` lines from ``path`` into the environment, keeping set ones.

    For service managers like launchd that can't load an env file themselves. A file
    that is missing or unreadable is an error, not a silent no-op.
    """
    if not path.is_file():
        raise ServeConfigError(f"--env-file {path} does not exist or is not a file")
    apply_env(dotenv_values(path))


def checkout_env_file(server: Server, package_dir: Path | None = None) -> Path | None:
    """The ``.env`` of the source checkout ``server`` runs from, or None.

    ``.env`` is a development convenience, so it is looked for in exactly one place:
    beside the project's own ``pyproject.toml``, two levels above the package
    (``<checkout>/src/<package>``). An installed copy sits in ``site-packages`` with
    no such file there, so it reads no ``.env`` at all: not one in a directory above
    where it is installed (``~/.env`` for a tool installed under the home directory),
    and never the working directory's, which is whatever project the MCP client has
    open. A ``.env`` can point a server at another store or widen what it shows, so
    only a file the operator chose may do that.
    """
    here = server.package_dir() if package_dir is None else package_dir
    if here is None:
        return None
    root = here.parent.parent
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project")
    except (OSError, ValueError):  # no such file, or not TOML: not a checkout
        return None
    if not isinstance(project, dict) or project.get("name") != server.name:
        return None
    env_file = root / ".env"
    return env_file if env_file.is_file() else None


def load_dotenv(server: Server, package_dir: Path | None = None) -> None:
    """Load a source checkout's ``.env`` by the same rule as an env file; else nothing."""
    path = checkout_env_file(server, package_dir)
    if path is not None:
        apply_env(dotenv_values(path))


def load_configuration(
    server: Server, env_file: Path | None, *, package_dir: Path | None = None
) -> None:
    """Load the named env file, or else a checkout's ``.env``: one, never both."""
    if env_file is not None:
        load_env_file(env_file)
    else:
        load_dotenv(server, package_dir)


def env_bool(name: str, default: bool) -> bool:
    """A yes, or anything else for no; the default when unset."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _YES


def env_switch(name: str) -> bool | None:
    """A protection that is on unless it is plainly switched off: True, False or None.

    Unset, or set to nothing, is on. Only a plain no is off. Anything else (a typo)
    is None: the caller keeps the protection on, and its commands refuse to start,
    so a misspelt value can pass for neither answer.
    """
    word = os.environ.get(name, "").strip().lower()
    if not word or word in _YES:
        return True
    if word in _NO:
        return False
    return None


def env_flag(name: str) -> bool | None:
    """A power that is off unless it plainly says yes: True, False or None for a typo.

    The opposite default of :func:`env_switch`, for something that stays off until
    the operator asks for it.
    """
    word = os.environ.get(name, "").strip().lower()
    if not word or word in _NO:
        return False
    if word in _YES:
        return True
    return None


def env_list(name: str) -> tuple[str, ...]:
    """A comma-separated variable as its entries, trimmed, without empty ones."""
    return tuple(entry.strip() for entry in os.environ.get(name, "").split(",") if entry.strip())


def set_but_blank(*names: str) -> tuple[str, ...]:
    """Those of ``names`` that are in the environment and list nothing."""
    return tuple(name for name in names if name in os.environ and not env_list(name))


def env_int(name: str, default: int, *, minimum: int, maximum: int | None = None) -> int | None:
    """A whole number from ``minimum`` to ``maximum``: the default when unset or empty.

    None when the value isn't a whole number in that range.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < minimum or (maximum is not None and value > maximum):
        return None
    return value


def env_seconds(name: str, default: float) -> float | None:
    """A positive, finite number of seconds: the default when unset or empty; else None."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return None
    if not value > 0 or value == float("inf"):  # also catches nan
        return None
    return value
