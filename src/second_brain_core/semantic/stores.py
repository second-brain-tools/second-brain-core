"""The two index stores: sqlite-vec (the default) and Chroma.

Every heavy import lives inside a constructor, so a missing extra surfaces as a
:class:`~second_brain_core.errors.SemanticUnavailableError` that says what to install.

**What the index file records about itself** (``meta`` in sqlite, the collection's
metadata in Chroma):

- ``schema``: the layout's version. A file from before it existed (second-brain-joplin
  1.2 and earlier, with ``note_id`` columns) is migrated in place by the sqlite store,
  in one transaction, with no re-embedding; the Chroma store rebuilds it by itself.
- ``source``: the server that owns it. A second server pointed at the same directory
  is refused: each one's sync would delete the other's items.
- ``model`` and ``dim``: the embedding model and the vectors' width. Either changing
  means a rebuild (the engine compares them with its embedder).

**The word index** (sqlite only) is SQLite FTS5 over the same chunks, with the
``unicode61 remove_diacritics 2`` tokenizer: case and accents are ignored, and a line
break is a space. A word hyphenated across a line break (``embed-`` / ``ding``) is
indexed both as written and joined (``embedding``), so a real hyphen is never lost.
A SQLite built without FTS5 has no word index; search by meaning still works.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, cast

from ..errors import SemanticUnavailableError, WordSearchUnavailableError
from ..server import Server
from .types import ChunkRecord, QueryHit, VectorIndex

SCHEMA = 2
STORES = ("sqlite-vec", "chroma")
_HYPHEN_BREAK = re.compile(r"(\w+)-[ \t]*\r?\n[ \t]*(\w+)")
_WORD = re.compile(r"\w+")


def install_hint(server: Server, package: str, extra: str = "semantic") -> str:
    return (
        f"Search by meaning needs the optional '{package}' dependency. Install it with "
        f'`pip install "{server.name}[{extra}]"` (or the matching uvx/uv extra).'
    )


def no_extensions_hint(server: Server) -> str:
    return (
        "The sqlite-vec index needs a Python that can load SQLite extensions, and this "
        "one can't (some macOS and pyenv builds leave it out). Run the server on uv's own "
        "Python (`uv python install 3.13`, then `uvx --python 3.13 ...`), or set "
        f"{server.var('VECTOR_STORE')}=chroma with the [semantic-chroma] extra."
    )


def owned_elsewhere(server: Server, index_dir: Path, owner: str) -> SemanticUnavailableError:
    return SemanticUnavailableError(
        f"The search index in {index_dir} belongs to {owner}. Give each server an index "
        f"directory of its own ({server.var('INDEX_DIR')})."
    )


def joined_words(text: str) -> str:
    """The words hyphenated across a line break in ``text``, joined.

    ``embed-`` at the end of one line and ``ding`` at the start of the next give
    ``embedding``.
    """
    return " ".join(a + b for a, b in _HYPHEN_BREAK.findall(text))


def match_expression(query: str) -> str | None:
    """``query`` as an FTS5 expression: every word, each quoted (no operators); None if none."""
    words = _WORD.findall(query)
    return " ".join(f'"{word}"' for word in words) if words else None


def build_vector_index(server: Server, store: str, index_dir: str | Path) -> VectorIndex:
    """Open (or create) the configured store in ``index_dir``, for ``server``."""
    directory = Path(index_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if store == "sqlite-vec":
        return SqliteVecIndex(directory, server)
    if store == "chroma":
        return ChromaVectorIndex(directory, server)
    raise SemanticUnavailableError(
        f"Unknown {server.var('VECTOR_STORE')} {store!r}; expected 'sqlite-vec' or 'chroma'."
    )


class SqliteVecIndex:
    """One SQLite file: chunk metadata, a vec0 table of vectors, and an FTS5 word index."""

    def __init__(self, index_dir: Path, server: Server) -> None:
        try:
            import sqlite_vec
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise SemanticUnavailableError(install_hint(server, "sqlite-vec")) from exc
        if not hasattr(sqlite3.Connection, "enable_load_extension"):
            # Some Python builds (macOS's framework Python, some pyenv builds) leave
            # out SQLite's extension loading, which sqlite-vec needs.
            raise SemanticUnavailableError(no_extensions_hint(server))
        self._conn = sqlite3.connect(str(index_dir / "index.db"), check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._serialize = sqlite_vec.serialize_float32
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        owner = self._meta("source")
        if owner and owner != server.name:
            self._conn.close()
            raise owned_elsewhere(server, index_dir, owner)
        self._words = _has_fts5(self._conn)
        if self._columns("chunks") and "note_id" in self._columns("chunks"):
            self._migrate_from_1()
        self._create_tables()
        if not owner:
            self._set_meta("source", server.name)
        self._set_meta("schema", str(SCHEMA))
        self._conn.commit()

    # -- the file's layout -------------------------------------------------------

    def _columns(self, table: str) -> list[str]:
        return [row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def _meta(self, key: str) -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return cast(str, row[0]) if row else ""

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _create_tables(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks("
            "rowid INTEGER PRIMARY KEY, chunk_id TEXT UNIQUE, item_id TEXT, "
            "version TEXT, title TEXT, chunk_index INTEGER, text TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS empty_items(item_id TEXT PRIMARY KEY, version TEXT)"
        )
        if self._words:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                "text, joined, tokenize = 'unicode61 remove_diacritics 2')"
            )

    def _migrate_from_1(self) -> None:
        """A second-brain-joplin 1.2 file: rename to the neutral layout, keep every vector.

        One transaction: a failure leaves the file exactly as it was.
        """
        conn = self._conn
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE chunks RENAME COLUMN note_id TO item_id")
            conn.execute("ALTER TABLE chunks RENAME COLUMN updated_time TO version")
            if self._columns("empty_notes"):
                conn.execute("ALTER TABLE empty_notes RENAME TO empty_items")
                conn.execute("ALTER TABLE empty_items RENAME COLUMN note_id TO item_id")
                conn.execute("ALTER TABLE empty_items RENAME COLUMN updated_time TO version")
            if self._vec_table_ready():
                row = conn.execute(
                    "SELECT vec_length(embedding) FROM vec_chunks LIMIT 1"
                ).fetchone()
                if row:
                    self._set_meta("dim", str(int(row[0])))
            self._create_tables()
            if self._words:
                rows = conn.execute("SELECT rowid, text FROM chunks").fetchall()
                conn.executemany(
                    "INSERT INTO chunks_fts(rowid, text, joined) VALUES(?, ?, ?)",
                    [(rowid, text, joined_words(text)) for rowid, text in rows],
                )
            self._set_meta("schema", str(SCHEMA))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @property
    def model_name(self) -> str:
        return self._meta("model")

    @property
    def dim(self) -> int:
        raw = self._meta("dim")
        return int(raw) if raw.isdigit() else 0

    def _vec_table_ready(self) -> bool:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'vec_chunks'"
        ).fetchone()
        return row is not None

    def _ensure_vec_table(self, dim: int) -> None:
        if not self._vec_table_ready():
            self._conn.execute(
                f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{dim}])"
            )
            self._set_meta("dim", str(dim))
            self._conn.commit()

    # -- the protocol ------------------------------------------------------------

    def item_versions(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT item_id, version FROM chunks").fetchall()
        return {item_id: version for item_id, version in rows}

    def empty_versions(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT item_id, version FROM empty_items").fetchall()
        return {item_id: version for item_id, version in rows}

    def mark_empty(self, item_id: str, version: str) -> None:
        self._conn.execute(
            "INSERT INTO empty_items(item_id, version) VALUES(?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET version = excluded.version",
            (item_id, version),
        )
        self._conn.commit()

    def upsert(self, records: list[ChunkRecord]) -> None:
        if not records:
            return
        self._ensure_vec_table(len(records[0].embedding))
        item_ids = sorted({record.item_id for record in records})
        self._conn.execute(
            f"DELETE FROM empty_items WHERE item_id IN ({', '.join('?' for _ in item_ids)})",
            item_ids,
        )
        for record in records:
            cursor = self._conn.execute(
                "INSERT INTO chunks(chunk_id, item_id, version, title, chunk_index, text) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    record.chunk_id,
                    record.item_id,
                    record.version,
                    record.title,
                    record.chunk_index,
                    record.text,
                ),
            )
            self._conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES(?, ?)",
                (cursor.lastrowid, self._serialize(record.embedding)),
            )
            if self._words:
                self._conn.execute(
                    "INSERT INTO chunks_fts(rowid, text, joined) VALUES(?, ?, ?)",
                    (cursor.lastrowid, record.text, joined_words(record.text)),
                )
        self._conn.commit()

    def delete_items(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        placeholders = ", ".join("?" for _ in item_ids)
        rows = self._conn.execute(
            f"SELECT rowid FROM chunks WHERE item_id IN ({placeholders})", item_ids
        ).fetchall()
        rowids = [r[0] for r in rows]
        self._conn.execute(f"DELETE FROM chunks WHERE item_id IN ({placeholders})", item_ids)
        self._conn.execute(f"DELETE FROM empty_items WHERE item_id IN ({placeholders})", item_ids)
        if rowids:
            rowid_placeholders = ", ".join("?" for _ in rowids)
            if self._vec_table_ready():
                self._conn.execute(
                    f"DELETE FROM vec_chunks WHERE rowid IN ({rowid_placeholders})", rowids
                )
            if self._words:
                self._conn.execute(
                    f"DELETE FROM chunks_fts WHERE rowid IN ({rowid_placeholders})", rowids
                )
        self._conn.commit()

    def query(self, embedding: list[float], k: int) -> list[QueryHit]:
        if not self._vec_table_ready():
            return []
        rows = self._conn.execute(
            "SELECT c.item_id, c.title, c.text, c.chunk_index, v.distance "
            "FROM vec_chunks v JOIN chunks c ON c.rowid = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (self._serialize(embedding), k),
        ).fetchall()
        return [
            QueryHit(
                item_id=item_id,
                title=title,
                text=text,
                chunk_index=chunk_index,
                score=1.0 / (1.0 + float(distance)),
            )
            for item_id, title, text, chunk_index, distance in rows
        ]

    def word_search(self, query: str, k: int) -> list[QueryHit]:
        if not self._words:
            raise WordSearchUnavailableError(
                "This SQLite has no FTS5, so the index has no word search. "
                "Search by meaning still works."
            )
        expression = match_expression(query)
        if expression is None:
            return []
        rows = self._conn.execute(
            "SELECT c.item_id, c.title, c.text, c.chunk_index, f.rank "
            "FROM chunks_fts f JOIN chunks c ON c.rowid = f.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY f.rank LIMIT ?",
            (expression, k),
        ).fetchall()
        # FTS5's rank is bm25: negative, and lower for a better match.
        return [
            QueryHit(
                item_id=item_id,
                title=title,
                text=text,
                chunk_index=chunk_index,
                score=-float(rank),
            )
            for item_id, title, text, chunk_index, rank in rows
        ]

    def reset(self, model_name: str) -> None:
        self._conn.execute("DROP TABLE IF EXISTS vec_chunks")
        self._conn.execute("DELETE FROM chunks")
        self._conn.execute("DELETE FROM empty_items")
        if self._words:
            self._conn.execute("DELETE FROM chunks_fts")
        self._conn.execute("DELETE FROM meta WHERE key = 'dim'")
        self._set_meta("model", model_name)
        self._conn.commit()


def _has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.fts5_probe")
    except sqlite3.OperationalError:
        return False
    return True


class ChromaVectorIndex:
    """A persistent Chroma collection of chunks, keyed by chunk id. No word search."""

    _COLLECTION = "items"
    # Items with no text. Chroma needs a vector on every record, so each mark carries a
    # one-number placeholder; it lives in its own collection, which no search reads.
    _EMPTY_COLLECTION = "empty_items"
    # A second-brain-joplin 1.2 index: rebuilt by itself (dropped here, then the
    # engine finds no model and embeds everything again).
    _OLD_COLLECTIONS = ("notes", "empty_notes")
    _PLACEHOLDER = [1.0]

    def __init__(self, index_dir: Path, server: Server) -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise SemanticUnavailableError(
                install_hint(server, "chromadb", extra="semantic-chroma")
            ) from exc
        self._server = server
        self._client = chromadb.PersistentClient(path=str(index_dir / "chroma"))
        existing = {c if isinstance(c, str) else c.name for c in self._client.list_collections()}
        if self._COLLECTION in existing:
            owner = (self._client.get_collection(self._COLLECTION).metadata or {}).get("source")
            if owner and owner != server.name:
                raise owned_elsewhere(server, index_dir, str(owner))
        for name in self._OLD_COLLECTIONS:
            if name in existing:
                self._client.delete_collection(name)
        self._open("")

    def _open(self, model_name: str) -> None:
        self._collection = self._client.get_or_create_collection(
            self._COLLECTION, metadata=self._new_metadata(model_name)
        )
        self._empty = self._client.get_or_create_collection(self._EMPTY_COLLECTION)

    def _new_metadata(self, model_name: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {"schema": SCHEMA, "source": self._server.name}
        if model_name:
            metadata["model"] = model_name
        return metadata

    @property
    def model_name(self) -> str:
        return cast(str, (self._collection.metadata or {}).get("model", ""))

    @property
    def dim(self) -> int:
        return int((self._collection.metadata or {}).get("dim", 0))

    def item_versions(self) -> dict[str, str]:
        data = self._collection.get(include=["metadatas"])
        return {
            str(metadata["item_id"]): str(metadata["version"])
            for metadata in data.get("metadatas") or []
        }

    def empty_versions(self) -> dict[str, str]:
        data = self._empty.get(include=["metadatas"])
        ids = data.get("ids") or []
        metadatas = data.get("metadatas") or []
        return {
            str(item_id): str(metadata["version"])
            for item_id, metadata in zip(ids, metadatas, strict=True)
        }

    def mark_empty(self, item_id: str, version: str) -> None:
        self._empty.upsert(
            ids=[item_id], embeddings=[self._PLACEHOLDER], metadatas=[{"version": version}]
        )

    def upsert(self, records: list[ChunkRecord]) -> None:
        if not records:
            return
        if not self.dim:
            metadata = dict(self._collection.metadata or {})
            metadata["dim"] = len(records[0].embedding)
            self._collection.modify(metadata=metadata)
        self._empty.delete(ids=sorted({r.item_id for r in records}))
        self._collection.upsert(
            ids=[r.chunk_id for r in records],
            embeddings=[r.embedding for r in records],
            documents=[r.text for r in records],
            metadatas=[
                {
                    "item_id": r.item_id,
                    "title": r.title,
                    "version": r.version,
                    "chunk_index": r.chunk_index,
                }
                for r in records
            ],
        )

    def delete_items(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        self._collection.delete(where={"item_id": {"$in": item_ids}})
        self._empty.delete(ids=item_ids)

    def query(self, embedding: list[float], k: int) -> list[QueryHit]:
        result: dict[str, Any] = self._collection.query(
            query_embeddings=[embedding],
            n_results=k,
            include=["metadatas", "documents", "distances"],
        )
        metadatas = (result.get("metadatas") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            QueryHit(
                item_id=str(metadata["item_id"]),
                title=str(metadata["title"]),
                text=str(document),
                chunk_index=int(metadata["chunk_index"]),
                score=1.0 / (1.0 + float(distance)),
            )
            for metadata, document, distance in zip(metadatas, documents, distances)
        ]

    def word_search(self, query: str, k: int) -> list[QueryHit]:
        raise WordSearchUnavailableError(
            "The Chroma store has no word search; the default store (sqlite-vec) has."
        )

    def reset(self, model_name: str) -> None:
        for name in (self._COLLECTION, self._EMPTY_COLLECTION):
            try:
                self._client.delete_collection(name)
            except Exception:  # noqa: BLE001 - the collection may not exist yet
                pass
        self._open(model_name)
