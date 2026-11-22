"""How a server describes itself to the shared pieces."""

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Server:
    """One MCP server built on this library.

    ``name`` is the project's name in its ``pyproject.toml`` (``second-brain-joplin``),
    ``prefix`` starts every one of its environment variables (``SBJ_``), and
    ``package`` is its import name (``second_brain_joplin``): its log lines are
    written under that logger, and its checkout is found from where that package
    lives. ``default_port`` is where its HTTP mode listens unless told otherwise.
    """

    name: str
    prefix: str
    package: str
    default_port: int

    def __post_init__(self) -> None:
        if not self.prefix.endswith("_") or not self.prefix[:-1].isidentifier():
            raise ValueError(f"the prefix must look like 'SBX_', got {self.prefix!r}")
        if not 1 <= self.default_port <= 65535:
            raise ValueError(f"the default port must be 1 to 65535, got {self.default_port}")

    def var(self, name: str) -> str:
        """The full name of one of this server's environment variables: ``var("PORT")``."""
        return f"{self.prefix}{name}"

    def logger(self, name: str) -> logging.Logger:
        """This server's logger ``<package>.<name>``, so its log set-up covers it."""
        return logging.getLogger(f"{self.package}.{name}")

    def package_dir(self) -> Path | None:
        """Where this server's package is installed or checked out, if it can be found."""
        spec = importlib.util.find_spec(self.package)
        if spec is None or spec.origin is None:
            return None
        return Path(spec.origin).resolve().parent
