"""The invisible-character check, on this library's own source and on planted files."""

from pathlib import Path

import pytest

from second_brain_core.testing import check_no_invisible_characters, invisible_characters

ROOT = Path(__file__).resolve().parent.parent


def test_no_source_file_here_holds_an_invisible_character() -> None:
    check_no_invisible_characters(ROOT, ("src/**/*.py", "tests/**/*.py", "scripts/**/*.py"))


@pytest.mark.parametrize(
    ("char", "code"),
    [
        ("\u200b", "U+200B"),  # zero-width space
        ("\u202e", "U+202E"),  # right-to-left override
        ("\u00a0", "U+00A0"),  # no-break space
        ("\u2028", "U+2028"),  # line separator
        ("\ufe0f", "U+FE0F"),  # variation selector
        ("\ue000", "U+E000"),  # private use
    ],
)
def test_a_planted_character_is_found_by_file_and_line(tmp_path: Path, char: str, code: str) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text(f"x = 1\ny = 'a{char}b'\n", encoding="utf-8")
    assert invisible_characters(tmp_path) == [f"src/mod.py:2 {code}"]
    with pytest.raises(AssertionError, match="src/mod.py:2"):
        check_no_invisible_characters(tmp_path)


def test_tabs_plain_spaces_and_escapes_pass(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("\tx = '\\u200b'  # escaped\n", encoding="utf-8")
    check_no_invisible_characters(tmp_path)


def test_only_the_named_patterns_are_read(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("a\u200bb\n", encoding="utf-8")
    assert invisible_characters(tmp_path) == []
    assert invisible_characters(tmp_path, ["*.txt"]) == ["notes.txt:1 U+200B"]


def test_a_windows_checkout_with_crlf_line_ends_passes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    check_no_invisible_characters(tmp_path)
