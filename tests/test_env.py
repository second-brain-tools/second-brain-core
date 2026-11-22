"""Where settings come from, and the readers that never raise."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fakeserver import OTHER, SERVER

from second_brain_core import env
from second_brain_core.errors import ServeConfigError

PLANTED = "SBF_FILTER=Everything\nSBF_STORE_URL=http://evil.example\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.startswith(("SBF_", "SBO_")):
            monkeypatch.delenv(name)
    yield


def _checkout(root: Path, *, name: str = "fake-server", env_text: str | None = PLANTED) -> Path:
    """A source checkout of a project called ``name``; returns its package directory."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    if env_text is not None:
        (root / ".env").write_text(env_text)
    package = root / "src" / "fakeserver"
    package.mkdir(parents=True)
    return package


# -- the env file and the checkout's .env -------------------------------------


def test_an_env_file_fills_in_only_what_is_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SBF_KEEP", "mine")
    monkeypatch.setenv("SBF_EMPTY", "")  # set to nothing: the file's value counts
    path = tmp_path / "serve.env"
    path.write_text("SBF_KEEP=theirs\nSBF_EMPTY=filled\nSBF_NEW=new\nSBF_BARE\n")
    env.load_env_file(path)
    assert os.environ["SBF_KEEP"] == "mine"
    assert os.environ["SBF_EMPTY"] == "filled"
    assert os.environ["SBF_NEW"] == "new"
    assert "SBF_BARE" not in os.environ  # a name with no value sets nothing


def test_a_missing_env_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ServeConfigError, match="does not exist or is not a file"):
        env.load_env_file(tmp_path / "nope.env")
    with pytest.raises(ServeConfigError, match="does not exist or is not a file"):
        env.load_env_file(tmp_path)  # a directory is not a file


def test_a_checkouts_dotenv_is_found_beside_its_own_pyproject(tmp_path: Path) -> None:
    package = _checkout(tmp_path / "clone")
    assert env.checkout_env_file(SERVER, package) == tmp_path / "clone" / ".env"


@pytest.mark.parametrize(
    "case", ["another-project", "no-dotenv", "no-pyproject", "not-toml", "no-project-table"]
)
def test_no_dotenv_unless_it_is_this_projects_checkout(tmp_path: Path, case: str) -> None:
    root = tmp_path / "clone"
    if case == "another-project":
        package = _checkout(root, name="something-else")
    elif case == "no-dotenv":
        package = _checkout(root, env_text=None)
    else:
        package = _checkout(root)
        toml = {"no-pyproject": None, "not-toml": "[[[", "no-project-table": "[tool.x]\n"}[case]
        if toml is None:
            (root / "pyproject.toml").unlink()
        else:
            (root / "pyproject.toml").write_text(toml)
    assert env.checkout_env_file(SERVER, package) is None


def test_another_servers_checkout_is_not_ours(tmp_path: Path) -> None:
    package = _checkout(tmp_path / "clone")
    assert env.checkout_env_file(OTHER, package) is None


def test_an_installed_server_reads_no_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither a .env above the install nor the working directory's counts."""
    site = tmp_path / "home" / "venv" / "lib" / "site-packages"
    package = site / "fakeserver"
    package.mkdir(parents=True)
    (tmp_path / "home" / ".env").write_text(PLANTED)
    working_directory = tmp_path / "some-project"
    working_directory.mkdir()
    (working_directory / ".env").write_text(PLANTED)
    monkeypatch.chdir(working_directory)
    env.load_dotenv(SERVER, package)
    assert "SBF_FILTER" not in os.environ
    assert "SBF_STORE_URL" not in os.environ


def test_the_real_package_dir_is_found() -> None:
    package_dir = SERVER.package_dir()
    assert package_dir is not None and (package_dir / "__init__.py").is_file()
    assert OTHER.package_dir() is None  # not installed anywhere
    assert env.checkout_env_file(OTHER) is None


def test_a_named_env_file_is_the_whole_configuration(tmp_path: Path) -> None:
    """A checkout's .env is not read as well: it would fill in what the file leaves out."""
    package = _checkout(tmp_path / "clone", env_text="SBF_FROM_DOTENV=yes\nSBF_FILTER=Work\n")
    named = tmp_path / "serve.env"
    named.write_text("SBF_FILTER=Inbox\n")
    env.load_configuration(SERVER, named, package_dir=package)
    assert os.environ["SBF_FILTER"] == "Inbox"
    assert "SBF_FROM_DOTENV" not in os.environ


def test_without_a_named_file_the_checkouts_dotenv_is_read(tmp_path: Path) -> None:
    package = _checkout(tmp_path / "clone", env_text="SBF_FROM_DOTENV=yes\n")
    env.load_configuration(SERVER, None, package_dir=package)
    assert os.environ["SBF_FROM_DOTENV"] == "yes"


# -- the readers ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, True), ("", True), ("yes", True), (" On ", True), ("no", False), ("0", False)]
    + [("nope", None), ("maybe", None)],
)
def test_env_switch(
    raw: str | None, expected: bool | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if raw is not None:
        monkeypatch.setenv("SBF_SWITCH", raw)
    assert env.env_switch("SBF_SWITCH") is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), ("", False), ("off", False), ("true", True), ("1", True), ("sure", None)],
)
def test_env_flag(raw: str | None, expected: bool | None, monkeypatch: pytest.MonkeyPatch) -> None:
    if raw is not None:
        monkeypatch.setenv("SBF_FLAG", raw)
    assert env.env_flag("SBF_FLAG") is expected


def test_env_bool(monkeypatch: pytest.MonkeyPatch) -> None:
    assert env.env_bool("SBF_BOOL", True) is True
    monkeypatch.setenv("SBF_BOOL", "nonsense")
    assert env.env_bool("SBF_BOOL", True) is False
    monkeypatch.setenv("SBF_BOOL", " YES ")
    assert env.env_bool("SBF_BOOL", False) is True


def test_env_list_and_set_but_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBF_LIST", " a, ,b ,, c ")
    monkeypatch.setenv("SBF_BLANK", " , ")
    assert env.env_list("SBF_LIST") == ("a", "b", "c")
    assert env.env_list("SBF_UNSET") == ()
    assert env.set_but_blank("SBF_LIST", "SBF_BLANK", "SBF_UNSET") == ("SBF_BLANK",)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 7), ("", 7), (" 5 ", 5), ("1", 1), ("10", 10)]
    + [("0", None), ("11", None), ("-3", None), ("2.5", None), ("lots", None)],
)
def test_env_int(raw: str | None, expected: int | None, monkeypatch: pytest.MonkeyPatch) -> None:
    if raw is not None:
        monkeypatch.setenv("SBF_INT", raw)
    assert env.env_int("SBF_INT", 7, minimum=1, maximum=10) == expected


def test_env_int_without_a_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBF_INT", "100000")
    assert env.env_int("SBF_INT", 0, minimum=0) == 100000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 2.5), ("", 2.5), ("0.5", 0.5), ("3", 3.0)]
    + [("0", None), ("-1", None), ("inf", None), ("nan", None), ("soon", None)],
)
def test_env_seconds(
    raw: str | None, expected: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if raw is not None:
        monkeypatch.setenv("SBF_SECONDS", raw)
    assert env.env_seconds("SBF_SECONDS", 2.5) == expected
