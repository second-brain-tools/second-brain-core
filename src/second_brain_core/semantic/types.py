"""Protocols and value objects of the search engine.

Nothing here imports an optional dependency: the stores and embedders implement
these protocols, so the engine is exercised in tests with in-memory fakes.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


@dataclass(frozen=True)
class ChunkRecord:
    """One embedded chunk of an item, ready to store in the index."""

    chunk_id: str
    item_id: str
    title: str
    version: str
    chunk_index: int
    text: str
    embedding: list[float]


@dataclass(frozen=True)
class QueryHit:
    """One result from the index: a chunk of an item, with its score (higher is better)."""

    item_id: str
    title: str
    text: str
    chunk_index: int
    score: float


@dataclass(frozen=True)
class SyncPlan:
    """The items to (re)embed and to drop, from a comparison of versions."""

    changed_ids: list[str]
    deleted_ids: list[str]


@dataclass(frozen=True)
class SyncStats:
    """What a sync did. Counts are of items with text; an empty item is never counted."""

    added: int
    updated: int
    deleted: int
    total: int
    model_name: str


@dataclass(frozen=True)
class ItemText:
    """An item's title and text, as the source read them."""

    title: str
    text: str


class Skip(Enum):
    """Why the source gives no text for an item it listed."""

    HIDDEN = "hidden"  # the filter hides it now: nothing is kept
    NOT_READY = "not-ready"  # its text can't be read yet (still encrypted): asked again next time


class Source(Protocol):
    """What a server tells the engine about its items. Implemented by each server.

    ``list_versions`` gives id → version for every item visible **now**; an item it
    leaves out is dropped from the index. If it raises, the index is left exactly as
    it was. ``get_item`` reads one item; it is asked again whether the item is still
    visible, since the item may have moved since the listing. ``filter_active`` says
    whether some items are hidden: then every search asks ``list_versions`` which
    items it may return, whatever the index holds.
    """

    @property
    def filter_active(self) -> bool: ...

    async def list_versions(self) -> dict[str, str]: ...
    async def get_item(self, item_id: str) -> ItemText | Skip: ...


class Embedder(Protocol):
    """Turns text into vectors."""

    @property
    def model_name(self) -> str: ...
    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class VectorIndex(Protocol):
    """A persistent store of chunks, searched by meaning (and, where it can, by words).

    ``model_name`` is the embedding model the stored vectors come from ("" for a
    fresh store) and ``dim`` their width (0 while unknown); :meth:`reset` clears
    everything and records a new model, so the engine can rebuild when either
    changes.

    An item with no text has no chunks, so it is recorded apart, as a mark that holds
    its id and version and nothing else: :meth:`mark_empty` sets one and
    :meth:`empty_versions` lists them. :meth:`item_versions` lists only items with
    chunks. :meth:`delete_items` and :meth:`reset` clear marks too, and :meth:`upsert`
    of an item's chunks clears its mark.

    :meth:`word_search` finds chunks by their words, ignoring case and accents. A
    store that can't raises :class:`~second_brain_core.errors.WordSearchUnavailableError`.
    """

    @property
    def model_name(self) -> str: ...
    @property
    def dim(self) -> int: ...

    def item_versions(self) -> dict[str, str]: ...
    def empty_versions(self) -> dict[str, str]: ...
    def mark_empty(self, item_id: str, version: str) -> None: ...
    def upsert(self, records: list[ChunkRecord]) -> None: ...
    def delete_items(self, item_ids: list[str]) -> None: ...
    def query(self, embedding: list[float], k: int) -> list[QueryHit]: ...
    def word_search(self, query: str, k: int) -> list[QueryHit]: ...
    def reset(self, model_name: str) -> None: ...
