"""The index stores, tested against one set of checks, plus what only sqlite does.

Each shared check runs on the in-memory fake, on the sqlite-vec store (the default;
a dev dependency, so CI runs it) and on the Chroma store (skipped unless
``chromadb`` is installed). The stores take ready-made vectors, so no model is
downloaded.
"""

import gc
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from fakeserver import OTHER, SERVER

from second_brain_core.errors import (
    IndexOwnedElsewhereError,
    SemanticUnavailableError,
    WordSearchUnavailableError,
)
from second_brain_core.semantic import stores
from second_brain_core.semantic.testing import FakeVectorIndex
from second_brain_core.semantic.types import ChunkRecord, VectorIndex

MODEL = "test-model"
CAN_LOAD_EXTENSIONS = hasattr(sqlite3.Connection, "enable_load_extension")
NO_EXTENSIONS = "this Python can't load SQLite extensions (GitHub's macOS runner)"

Opener = Callable[[Path], VectorIndex]


def _sqlite(index_dir: Path) -> VectorIndex:
    pytest.importorskip("sqlite_vec")
    if not CAN_LOAD_EXTENSIONS:
        pytest.skip(NO_EXTENSIONS)
    return stores.build_vector_index(SERVER, "sqlite-vec", index_dir)


def _chroma(index_dir: Path) -> VectorIndex:
    pytest.importorskip("chromadb")
    return stores.build_vector_index(SERVER, "chroma", index_dir)


def _fake(_index_dir: Path) -> VectorIndex:
    return FakeVectorIndex()


STORES = [
    pytest.param(_fake, id="fake"),
    pytest.param(_sqlite, id="sqlite-vec"),
    pytest.param(_chroma, id="chroma"),
]
PERSISTENT = [STORES[1], STORES[2]]


def _record(item_id: str, index: int, vector: list[float], version: str = "100") -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"{item_id}::{index}",
        item_id=item_id,
        title=f"title of {item_id}",
        version=version,
        chunk_index=index,
        text=f"text {index} of {item_id}",
        embedding=vector,
    )


@pytest.fixture(params=STORES)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> VectorIndex:
    opener: Opener = request.param
    index = opener(tmp_path)
    index.reset(MODEL)
    return index


def test_a_fresh_store_is_empty(store: VectorIndex) -> None:
    assert store.model_name == MODEL
    assert store.dim == 0
    assert store.item_versions() == {}
    assert store.empty_versions() == {}
    assert store.query([1.0, 0.0, 0.0, 0.0], 5) == []


def test_upsert_then_query_returns_the_nearest_first(store: VectorIndex) -> None:
    store.upsert(
        [
            _record("a", 0, [1.0, 0.0, 0.0, 0.0]),
            _record("a", 1, [0.0, 1.0, 0.0, 0.0]),
            _record("b", 0, [0.0, 0.0, 1.0, 0.0], version="200"),
        ]
    )
    assert store.item_versions() == {"a": "100", "b": "200"}
    assert store.dim == 4
    hits = store.query([0.0, 0.0, 1.0, 0.0], 2)
    assert [(hit.item_id, hit.chunk_index) for hit in hits][0] == ("b", 0)
    assert hits[0].title == "title of b"
    assert hits[0].text == "text 0 of b"
    assert hits[0].score >= hits[1].score


def test_delete_items_removes_every_chunk_of_an_item(store: VectorIndex) -> None:
    store.upsert([_record("a", 0, [1.0, 0.0, 0.0, 0.0]), _record("a", 1, [0.0, 1.0, 0.0, 0.0])])
    store.upsert([_record("b", 0, [0.0, 0.0, 1.0, 0.0])])
    store.delete_items(["a"])
    assert store.item_versions() == {"b": "100"}
    assert {hit.item_id for hit in store.query([1.0, 0.0, 0.0, 0.0], 5)} == {"b"}
    store.delete_items([])  # nothing to do, no error


def test_a_mark_holds_a_version_apart_from_the_chunks(store: VectorIndex) -> None:
    store.mark_empty("e", "100")
    assert store.empty_versions() == {"e": "100"}
    assert store.item_versions() == {}
    store.mark_empty("e", "200")  # a newer version replaces the old
    assert store.empty_versions() == {"e": "200"}


def test_a_mark_is_never_a_search_result(store: VectorIndex) -> None:
    store.mark_empty("e", "100")
    store.upsert([_record("a", 0, [1.0, 0.0, 0.0, 0.0])])
    assert {hit.item_id for hit in store.query([1.0, 0.0, 0.0, 0.0], 5)} == {"a"}


def test_upsert_of_an_item_clears_its_mark(store: VectorIndex) -> None:
    store.mark_empty("e", "100")
    store.upsert([_record("e", 0, [1.0, 0.0, 0.0, 0.0], version="200")])
    assert store.empty_versions() == {}
    assert store.item_versions() == {"e": "200"}


def test_delete_items_clears_marks_too(store: VectorIndex) -> None:
    store.mark_empty("e", "100")
    store.mark_empty("f", "100")
    store.delete_items(["e"])
    assert store.empty_versions() == {"f": "100"}


def test_reset_clears_chunks_marks_and_width_and_records_the_model(store: VectorIndex) -> None:
    store.upsert([_record("a", 0, [1.0, 0.0, 0.0, 0.0])])
    store.mark_empty("e", "100")
    store.reset("another-model")
    assert store.model_name == "another-model"
    assert store.dim == 0
    assert store.item_versions() == {}
    assert store.empty_versions() == {}
    assert store.query([1.0, 0.0, 0.0, 0.0], 5) == []
    store.upsert([_record("a", 0, [1.0, 0.0])])  # a new width after the reset
    assert store.dim == 2


@pytest.mark.parametrize("opener", PERSISTENT)
def test_chunks_marks_and_width_survive_reopening_the_index(opener: Opener, tmp_path: Path) -> None:
    first = opener(tmp_path)
    first.reset(MODEL)
    first.upsert([_record("a", 0, [1.0, 0.0, 0.0, 0.0])])
    first.mark_empty("e", "100")

    again = opener(tmp_path)
    assert again.model_name == MODEL
    assert again.dim == 4
    assert again.item_versions() == {"a": "100"}
    assert again.empty_versions() == {"e": "100"}


@pytest.mark.parametrize("store_name", ["sqlite-vec", "chroma"])
def test_another_server_is_refused_the_same_directory(store_name: str, tmp_path: Path) -> None:
    opener = _sqlite if store_name == "sqlite-vec" else _chroma
    opener(tmp_path).reset(MODEL)
    with pytest.raises(IndexOwnedElsewhereError, match="belongs to fake-server") as refused:
        stores.build_vector_index(OTHER, store_name, tmp_path)
    assert "SBO_INDEX_DIR" in str(refused.value)
    assert str(tmp_path) not in str(refused.value)  # a path can name the operator's home


def test_an_unknown_store_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SemanticUnavailableError, match="Unknown SBF_VECTOR_STORE 'faiss'"):
        stores.build_vector_index(SERVER, "faiss", tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX modes")
def test_a_new_index_directory_is_private(tmp_path: Path) -> None:
    with pytest.raises(SemanticUnavailableError):  # made before the store is chosen
        stores.build_vector_index(SERVER, "faiss", tmp_path / "idx")
    assert (tmp_path / "idx").stat().st_mode & 0o777 == 0o700 & ~_umask()


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


# -- the word index (sqlite) ---------------------------------------------------------


WRAPPED = (
    "La réunion du Comité, about the embed-\n  ding pipe-\nline and a self-\ncontained "
    "plan that wraps\nacross lines."
)


def _with_text(index: VectorIndex, item_id: str, text: str) -> None:
    index.upsert(
        [
            ChunkRecord(
                chunk_id=f"{item_id}::0",
                item_id=item_id,
                title=item_id,
                version="1",
                chunk_index=0,
                text=text,
                embedding=[1.0, 0.0, 0.0, 0.0],
            )
        ]
    )


@pytest.fixture
def words(tmp_path: Path) -> VectorIndex:
    index = _sqlite(tmp_path)
    index.reset(MODEL)
    _with_text(index, "wrapped", WRAPPED)
    _with_text(index, "other", "nothing to see here")
    return index


@pytest.mark.parametrize(
    "query",
    [
        "reunion",  # an accent, folded
        "RÉUNION comite",  # case and accents both ways
        "wraps across",  # a phrase across a line break
        "embedding",  # hyphenated across a line break: joined
        "embed ding",  # and as written
        "pipeline",
        "self contained",  # a real hyphen at a line end: kept as written too
        "selfcontained",
    ],
)
def test_word_search_finds(words: VectorIndex, query: str) -> None:
    assert [hit.item_id for hit in words.word_search(query, 5)] == ["wrapped"]


@pytest.mark.parametrize("query", ["reunion missing", "", "  ;; ", 'AND OR NOT "'])
def test_word_search_finds_nothing(words: VectorIndex, query: str) -> None:
    assert words.word_search(query, 5) == []


def test_word_search_keeps_in_step_with_deletes_and_resets(words: VectorIndex) -> None:
    words.delete_items(["wrapped"])
    assert words.word_search("reunion", 5) == []
    words.reset(MODEL)
    assert words.word_search("nothing", 5) == []


def test_a_sqlite_without_fts5_has_no_word_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stores, "_has_fts5", lambda conn: False)
    index = _sqlite(tmp_path)
    index.reset(MODEL)
    _with_text(index, "a", "reunion")
    assert index.item_versions() == {"a": "1"}  # search by meaning still works
    assert index.query([1.0, 0.0, 0.0, 0.0], 1)
    with pytest.raises(WordSearchUnavailableError, match="no FTS5"):
        index.word_search("reunion", 5)
    index.delete_items(["a"])
    index.reset(MODEL)


def test_chroma_says_it_has_no_word_search(tmp_path: Path) -> None:
    index = _chroma(tmp_path)
    with pytest.raises(WordSearchUnavailableError, match="Chroma"):
        index.word_search("anything", 5)


# -- a file from second-brain-joplin 1.2 ----------------------------------------------


def _old_index(index_dir: Path) -> None:
    """The layout second-brain-joplin 1.2 wrote: note_id columns, empty_notes, no schema."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    if not CAN_LOAD_EXTENSIONS:
        pytest.skip(NO_EXTENSIONS)
    conn = sqlite3.connect(str(index_dir / "index.db"))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta(key, value) VALUES('model', ?)", (MODEL,))
    conn.execute(
        "CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE, note_id TEXT, "
        "updated_time TEXT, title TEXT, chunk_index INTEGER, text TEXT)"
    )
    conn.execute("CREATE TABLE empty_notes(note_id TEXT PRIMARY KEY, updated_time TEXT)")
    conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[4])")
    for rowid, (note, text) in enumerate([("a", "la réunion"), ("b", "the garden")], start=1):
        conn.execute(
            "INSERT INTO chunks(rowid, chunk_id, note_id, updated_time, title, chunk_index, text) "
            "VALUES(?, ?, ?, '100', ?, 0, ?)",
            (rowid, f"{note}::0", note, f"title of {note}", text),
        )
        conn.execute(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
            (rowid, sqlite_vec.serialize_float32([float(rowid == 1), float(rowid == 2), 0.0, 0.0])),
        )
    conn.execute("INSERT INTO empty_notes(note_id, updated_time) VALUES('e', '100')")
    conn.commit()
    conn.close()


def test_a_1_2_index_is_migrated_in_place_with_every_vector(tmp_path: Path) -> None:
    _old_index(tmp_path)
    index = _sqlite(tmp_path)
    assert index.model_name == MODEL  # kept: no rebuild, no re-embedding
    assert index.dim == 4
    assert index.item_versions() == {"a": "100", "b": "100"}
    assert index.empty_versions() == {"e": "100"}
    assert [hit.item_id for hit in index.query([1.0, 0.0, 0.0, 0.0], 1)] == ["a"]
    assert [hit.item_id for hit in index.word_search("reunion", 5)] == ["a"]
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    conn.close()
    assert meta == {"model": MODEL, "dim": "4", "schema": "2", "source": "fake-server"}
    # Opening it again changes nothing.
    again = _sqlite(tmp_path)
    assert again.item_versions() == {"a": "100", "b": "100"}
    assert len(again.word_search("garden", 5)) == 1


def test_a_failed_migration_leaves_the_file_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old_index(tmp_path)

    def broken(text: str) -> str:
        raise RuntimeError("the disk filled up")

    monkeypatch.setattr(stores, "joined_words", broken)
    with pytest.raises(RuntimeError, match="disk filled up"):
        _sqlite(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    columns = [row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master").fetchall()}
    conn.close()
    assert "note_id" in columns and "empty_notes" in tables and "chunks_fts" not in tables
    monkeypatch.undo()
    assert _sqlite(tmp_path).item_versions() == {"a": "100", "b": "100"}  # and it still migrates


def test_the_no_extensions_refusal_names_the_ways_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("sqlite_vec")
    no_extensions = SimpleNamespace(Connection=type("NoExtensions", (), {}))
    monkeypatch.setattr(stores, "sqlite3", no_extensions)
    with pytest.raises(SemanticUnavailableError, match="can load SQLite extensions") as refused:
        stores.SqliteVecIndex(tmp_path, SERVER)
    assert "SBF_VECTOR_STORE=chroma" in str(refused.value)


def test_a_migration_another_process_finished_is_left_alone(tmp_path: Path) -> None:
    """Two processes open a 1.2 file at once: the second finds it migrated, and stops."""
    _old_index(tmp_path)
    index = _sqlite(tmp_path)
    assert isinstance(index, stores.SqliteVecIndex)
    index._migrate_from_1()  # as the second process would, after waiting for the lock
    assert index.item_versions() == {"a": "100", "b": "100"}
    assert len(index.word_search("garden", 5)) == 1  # the word index wasn't filled twice


def test_an_empty_1_2_index_still_records_its_width(tmp_path: Path) -> None:
    _old_index(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    conn.execute("DELETE FROM chunks")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    conn.enable_load_extension(True)
    import sqlite_vec

    sqlite_vec.load(conn)
    conn.execute("DELETE FROM vec_chunks")
    conn.commit()
    conn.close()
    assert _sqlite(tmp_path).dim == 4


def test_an_interrupted_reset_leaves_the_index_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = _sqlite(tmp_path)
    index.reset(MODEL)
    _with_text(index, "a", "kept")
    assert isinstance(index, stores.SqliteVecIndex)

    def broken(key: str, value: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(index, "_set_meta", broken)
    with pytest.raises(sqlite3.OperationalError):
        index.reset("another-model")
    monkeypatch.undo()
    assert index.model_name == MODEL and index.dim == 4
    assert index.item_versions() == {"a": "1"}
    assert [hit.item_id for hit in index.query([1.0, 0.0, 0.0, 0.0], 1)] == ["a"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX modes")
def test_an_index_made_before_the_rule_is_made_private(tmp_path: Path) -> None:
    directory = tmp_path / "idx"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    _sqlite(directory).reset(MODEL)
    (directory / "index.db").chmod(0o644)
    directory.chmod(0o755)
    stores.build_vector_index(SERVER, "sqlite-vec", directory)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "index.db").stat().st_mode & 0o777 == 0o600


def test_a_refused_open_closes_its_connection(tmp_path: Path) -> None:
    owner = _sqlite(tmp_path)
    owner.reset(MODEL)
    del owner  # the owner's own connection closes with it
    gc.collect()
    for _ in range(3):
        with pytest.raises(IndexOwnedElsewhereError):
            stores.build_vector_index(OTHER, "sqlite-vec", tmp_path)
    # The file is not held open: it can be removed and made again.
    (tmp_path / "index.db").unlink()
    assert _sqlite(tmp_path).item_versions() == {}
