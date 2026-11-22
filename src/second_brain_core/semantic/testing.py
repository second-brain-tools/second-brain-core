"""In-memory stand-ins for the engine's parts, for any server's tests.

Deterministic and dependency-free: an embedder over token hashes, a vector index,
an index another process writes to, and a source of items held in a dict.
"""

import hashlib
import math
import unicodedata
from dataclasses import dataclass, field

from .types import ChunkRecord, ItemText, QueryHit, Skip

DIM = 16


def _stable_hash(token: str) -> int:
    return int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "big")


def fake_embedding(text: str) -> list[float]:
    """Bag of tokens hashed into a fixed-width, L2-normalised vector."""
    vec = [0.0] * DIM
    for token in text.lower().split():
        vec[_stable_hash(token) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return vec if norm == 0.0 else [v / norm for v in vec]


def _folded_words(text: str) -> set[str]:
    """Words without case or accents, as the sqlite store's word index sees them."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return {"".join(ch for ch in word if ch.isalnum()) for word in plain.split()} - {""}


class FakeEmbedder:
    """A deterministic embedder over token hashes."""

    def __init__(self, model_name: str = "fake-model", dim: int = DIM) -> None:
        self.model_name = model_name
        self.dim = dim
        self.doc_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.doc_calls += 1
        return [self._fit(fake_embedding(text)) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._fit(fake_embedding(text))

    def _fit(self, vec: list[float]) -> list[float]:
        return (vec + [0.0] * self.dim)[: self.dim]


class FakeVectorIndex:
    """An in-memory index keyed by chunk id, with a simple word search."""

    def __init__(self) -> None:
        self.model_name = ""
        self.dim = 0
        self._records: dict[str, ChunkRecord] = {}
        self._empty: dict[str, str] = {}

    def item_versions(self) -> dict[str, str]:
        return {r.item_id: r.version for r in self._records.values()}

    def empty_versions(self) -> dict[str, str]:
        return dict(self._empty)

    def mark_empty(self, item_id: str, version: str) -> None:
        self._empty[item_id] = version

    def stored_text(self) -> str:
        """Everything a reader of the index could find in it: titles and chunk text."""
        return "\n".join(f"{r.title}\n{r.text}" for r in self._records.values())

    def upsert(self, records: list[ChunkRecord]) -> None:
        for record in records:
            if not self.dim:
                self.dim = len(record.embedding)
            self._records[record.chunk_id] = record
            self._empty.pop(record.item_id, None)

    def delete_items(self, item_ids: list[str]) -> None:
        drop = set(item_ids)
        self._records = {cid: r for cid, r in self._records.items() if r.item_id not in drop}
        self._empty = {iid: v for iid, v in self._empty.items() if iid not in drop}

    def _hit(self, record: ChunkRecord, score: float) -> QueryHit:
        return QueryHit(
            item_id=record.item_id,
            title=record.title,
            text=record.text,
            chunk_index=record.chunk_index,
            score=score,
        )

    def query(self, embedding: list[float], k: int) -> list[QueryHit]:
        scored = [
            self._hit(r, sum(x * y for x, y in zip(embedding, r.embedding, strict=False)))
            for r in self._records.values()
        ]
        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:k]

    def word_search(self, query: str, k: int) -> list[QueryHit]:
        wanted = _folded_words(query)
        if not wanted:
            return []
        found = [
            self._hit(r, float(len(wanted)))
            for r in self._records.values()
            if wanted <= _folded_words(r.text)
        ]
        return found[:k]

    def reset(self, model_name: str) -> None:
        self.model_name = model_name
        self.dim = 0
        self._records = {}
        self._empty = {}


class SharedVectorIndex(FakeVectorIndex):
    """An index another process writes to: ``intruders`` appear just before each search.

    The real index is a file any process on the machine can sync into with its own
    settings, so a search can't assume it holds only what this process put there.
    """

    def __init__(self) -> None:
        super().__init__()
        self.intruders: list[ChunkRecord] = []

    def query(self, embedding: list[float], k: int) -> list[QueryHit]:
        self.upsert(self.intruders)
        return super().query(embedding, k)

    def word_search(self, query: str, k: int) -> list[QueryHit]:
        self.upsert(self.intruders)
        return super().word_search(query, k)


@dataclass
class FakeItem:
    title: str
    text: str
    version: str = "1"
    visible: bool = True
    ready: bool = True


@dataclass
class FakeSource:
    """A source of items held in a dict: an adapter as small as a server's can be.

    ``filter_active`` says whether some items are hidden. ``hide_on_read`` hides an
    item between the listing and the read, as an item that moved meanwhile is.
    ``down`` makes the listing fail, as a store that isn't answering does.
    """

    items: dict[str, FakeItem] = field(default_factory=dict)
    filter_active: bool = False
    hide_on_read: set[str] = field(default_factory=set)
    down: bool = False
    listings: int = 0
    reads: list[str] = field(default_factory=list)

    async def list_versions(self) -> dict[str, str]:
        self.listings += 1
        if self.down:
            raise ConnectionError("the fake store is down")
        return {iid: item.version for iid, item in self.items.items() if item.visible}

    async def get_item(self, item_id: str) -> ItemText | Skip:
        self.reads.append(item_id)
        item = self.items[item_id]
        if item_id in self.hide_on_read or not item.visible:
            return Skip.HIDDEN
        if not item.ready:
            return Skip.NOT_READY
        return ItemText(title=item.title, text=item.text)
