"""The server description, the package's import-time pin, and the small helpers."""

import logging
import os
from pathlib import Path

import pytest
from fakeserver import SERVER

import second_brain_core
from second_brain_core.recent import Recent, request_key
from second_brain_core.server import Server
from second_brain_core.testing import check_dotenv_cannot_set_fastmcp, check_each_imports_alone


def test_var_and_logger() -> None:
    assert SERVER.var("PORT") == "SBF_PORT"
    assert SERVER.logger("audit") is logging.getLogger("fakeserver.audit")


@pytest.mark.parametrize("prefix", ["SBF", "sbf-", "_", "1X_", ""])
def test_a_prefix_must_look_like_one(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        Server(name="x", prefix=prefix, package="x", default_port=1)


@pytest.mark.parametrize("port", [0, 65536])
def test_the_default_port_must_be_a_port(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        Server(name="x", prefix="X_", package="x", default_port=port)


def test_importing_the_package_pins_fastmcps_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FASTMCP_ENV_FILE")
    second_brain_core.pin_fastmcp_env()
    assert os.environ["FASTMCP_ENV_FILE"] == os.devnull
    monkeypatch.setenv("FASTMCP_ENV_FILE", "/etc/chosen.env")  # the operator's choice
    second_brain_core.pin_fastmcp_env()
    assert os.environ["FASTMCP_ENV_FILE"] == "/etc/chosen.env"


# -- the checks a server runs on itself, run here on the fake server ------------


TESTS = Path(__file__).resolve().parent


def test_a_working_directorys_dotenv_cannot_set_fastmcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(TESTS))  # where fakeserver lives
    check_dotenv_cannot_set_fastmcp("fakeserver", tmp_path)


def test_the_check_catches_a_server_that_forgot_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A package that imports fastmcp before second_brain_core fails the check."""
    package = tmp_path / "lib" / "forgetful"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("import fastmcp  # noqa: F401\n")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "lib"))
    with pytest.raises(AssertionError, match="set FastMCP's transport to 'http'"):
        check_dotenv_cannot_set_fastmcp("forgetful", work)


def test_each_module_imports_alone() -> None:
    check_each_imports_alone(
        f"second_brain_core.{name}"
        for name in (
            "annotations",
            "audit",
            "auth",
            "env",
            "errors",
            "failure",
            "health",
            "recent",
            "remote",
            "server",
            "testing",
            "textclean",
            "semantic.chunking",
            "semantic.embedders",
            "semantic.engine",
            "semantic.stores",
            "semantic.sync",
            "semantic.testing",
            "semantic.types",
        )
    )


def test_a_module_that_fails_to_import_alone_is_named() -> None:
    with pytest.raises(AssertionError, match="import no_such_module_here failed on its own"):
        check_each_imports_alone(["no_such_module_here"])


# -- the repeat memory -----------------------------------------------------------


def test_recent_remembers_for_a_while_then_forgets() -> None:
    now = [0.0]
    recent: Recent[str] = Recent(seconds=10, at_most=2, clock=lambda: now[0])
    recent.put("a", "first")
    assert recent.get("a") == "first"
    now[0] = 10.5
    assert recent.get("a") is None
    recent.put("a", "1")
    recent.put("b", "2")
    recent.put("c", "3")  # full: the oldest goes
    assert (recent.get("a"), recent.get("b"), recent.get("c")) == (None, "2", "3")
    recent.forget("b")
    assert recent.get("b") is None
    recent.forget("never-there")


def test_request_key_keeps_parts_apart() -> None:
    assert request_key("ab", "c") != request_key("a", "bc")
    assert request_key("x", "y") == request_key("x", "y")
    assert len(request_key()) == 64
