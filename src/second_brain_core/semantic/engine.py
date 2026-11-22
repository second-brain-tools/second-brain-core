"""Keeps a server's index in step with its source, and searches it.

Pure orchestration: it takes a :class:`~.types.Source`, an :class:`~.types.Embedder`
and a :class:`~.types.VectorIndex` by injection and never imports an optional
dependency. Embedder and store calls run on worker threads, a lock keeps two syncs
from writing the store at once, and a semaphore caps the searches that embed and
query together.

The source decides what the index may hold: an item it hides is never embedded,
and one that becomes hidden is dropped by the next sync, before anything new is
embedded.

What a search returns does not rest on the index being clean, though. The index is
a file on disk that other processes can write, each with settings of its own (a
reindex run without the server's env file may have no filter and embed
everything). So with a filter active, every search asks the source which items are
visible now and returns only those, whatever the index holds.
"""

import asyncio
from enum import Enum

from .chunking import chunk_text
from .sync import diff_versions
from .types import ChunkRecord, Embedder, ItemText, QueryHit, Skip, Source, SyncStats, VectorIndex

# Over-fetch factor, so keeping one hit per item still yields up to `limit` items.
QUERY_OVERFETCH = 4
# Searches that may embed and query at the same time.
SEARCH_CONCURRENCY = 2


class _Embedded(Enum):
    """What became of one item a sync looked at."""

    TEXT = "text"  # its chunks are in the index
    EMPTY = "empty"  # it has no text: marked, so the next sync leaves it alone
    HIDDEN = "hidden"  # the source hides it now: nothing kept
    NOT_READY = "not-ready"  # not readable yet: not marked, so the next sync looks again


class Engine:
    """Keeps the index in step with the source and answers searches."""

    def __init__(
        self,
        source: Source,
        embedder: Embedder,
        index: VectorIndex,
        *,
        auto_sync: bool = True,
    ) -> None:
        self._source = source
        self._embedder = embedder
        self._index = index
        self._auto_sync = auto_sync
        self._lock = asyncio.Lock()
        # Query embedding runs on worker threads; a remote caller firing many
        # searches at once could otherwise tie up the model and the CPU.
        self._search_slots = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def sync(self, *, full: bool = False) -> SyncStats:
        """Re-embed changed items (or rebuild everything)."""
        async with self._lock:
            stats, _ = await self._sync_locked(full=full)
            return stats

    async def prune(self) -> int:
        """Drop indexed items that are hidden or gone, embedding nothing.

        The cheap half of a sync (one listing, no model). Returns how many items
        with text left the index.
        """
        current = await self._source.list_versions()
        async with self._lock:
            return await self._drop_missing(current)

    async def _drop_missing(self, current: dict[str, str]) -> int:
        """Remove every item that isn't in ``current``. Call with the lock."""
        indexed = await asyncio.to_thread(self._index.item_versions)
        empty = await asyncio.to_thread(self._index.empty_versions)
        gone = [item_id for item_id in {**empty, **indexed} if item_id not in current]
        if gone:
            await asyncio.to_thread(self._index.delete_items, gone)
        return sum(1 for item_id in gone if item_id in indexed)

    def _needs_rebuild(self) -> bool:
        """The stored vectors come from another model, or have another width."""
        if self._index.model_name != self._embedder.model_name:
            return True
        return bool(self._index.dim) and self._index.dim != self._embedder.dim

    async def _sync_locked(self, *, full: bool) -> tuple[SyncStats, set[str]]:
        """Sync, and say which items were visible when it looked. Call with the lock."""
        # Ask the source first: if it is down, or its filter doesn't resolve, the
        # index is left exactly as it was (a full rebuild included).
        current = await self._source.list_versions()

        model = self._embedder.model_name
        if full or self._needs_rebuild():
            await asyncio.to_thread(self._index.reset, model)
            indexed: dict[str, str] = {}
            empty: dict[str, str] = {}
        else:
            indexed = await asyncio.to_thread(self._index.item_versions)
            empty = await asyncio.to_thread(self._index.empty_versions)

        # An item with no text was looked at once for its version and marked, so it
        # isn't read again until it changes.
        plan = diff_versions(current, {**empty, **indexed})

        # Drop before embedding: an item that is now hidden must leave the index even
        # if an embedding below fails.
        if plan.deleted_ids:
            await asyncio.to_thread(self._index.delete_items, plan.deleted_ids)

        added = 0
        updated = 0
        for item_id in plan.changed_ids:
            outcome = await self._embed_item(item_id, current[item_id])
            if item_id in indexed:
                # Its text changed, or it was emptied: either way its entry did.
                if outcome in (_Embedded.TEXT, _Embedded.EMPTY):
                    updated += 1
            elif outcome is _Embedded.TEXT:  # new, or an empty item gained text
                added += 1

        stats = SyncStats(
            added=added,
            updated=updated,
            deleted=sum(1 for item_id in plan.deleted_ids if item_id in indexed),
            total=len(current),
            model_name=model,
        )
        return stats, set(current)

    async def _embed_item(self, item_id: str, version: str) -> _Embedded:
        """(Re)embed one item, or mark it as having no text, or leave it out."""
        item = await self._source.get_item(item_id)

        # Drop any stale chunks (and mark) first, so an item that shrank leaves
        # nothing behind.
        await asyncio.to_thread(self._index.delete_items, [item_id])
        if item is Skip.HIDDEN:
            return _Embedded.HIDDEN
        if item is Skip.NOT_READY:
            return _Embedded.NOT_READY
        assert isinstance(item, ItemText)

        chunks = chunk_text(item.title, item.text)
        if not chunks:
            await asyncio.to_thread(self._index.mark_empty, item_id, version)
            return _Embedded.EMPTY

        embeddings = await asyncio.to_thread(self._embedder.embed_documents, chunks)
        records = [
            ChunkRecord(
                chunk_id=f"{item_id}::{i}",
                item_id=item_id,
                title=item.title,
                version=version,
                chunk_index=i,
                text=text,
                embedding=embedding,
            )
            for i, (text, embedding) in enumerate(zip(chunks, embeddings, strict=True))
        ]
        await asyncio.to_thread(self._index.upsert, records)
        return _Embedded.TEXT

    async def _visible_for_search(self) -> set[str] | None:
        """Bring the index up to date for a search; the visible ids, or None for all.

        With auto-sync, a sync. Without it, nothing, unless a filter is active: the
        filter still has to hold now, so the source is asked which items are
        visible, and the index is tidied if no sync is using it (a running sync
        tidies it anyway, and a search shouldn't wait behind a long one).
        """
        if self._auto_sync:
            async with self._lock:
                _, visible = await self._sync_locked(full=False)
            return visible if self._source.filter_active else None
        if not self._source.filter_active:
            return None
        current = await self._source.list_versions()
        if not self._lock.locked():
            async with self._lock:
                await self._drop_missing(current)
        return set(current)

    @staticmethod
    def _best(hits: list[QueryHit], visible: set[str] | None, limit: int) -> list[QueryHit]:
        # Trust the source's answer, not the index's contents: another process may
        # have written hidden items into the same index since it was tidied.
        if visible is not None:
            hits = [hit for hit in hits if hit.item_id in visible]
        best: dict[str, QueryHit] = {}
        for hit in hits:  # hits are ordered best-first
            best.setdefault(hit.item_id, hit)
        return list(best.values())[:limit]

    async def search(self, query: str, limit: int = 10) -> list[QueryHit]:
        """Up to ``limit`` items closest in meaning to ``query``: each one's best chunk."""
        visible = await self._visible_for_search()
        async with self._search_slots:
            embedding = await asyncio.to_thread(self._embedder.embed_query, query)
            hits = await asyncio.to_thread(self._index.query, embedding, limit * QUERY_OVERFETCH)
        return self._best(hits, visible, limit)

    async def word_search(self, query: str, limit: int = 10) -> list[QueryHit]:
        """Up to ``limit`` items with every word of ``query``, ignoring case and accents.

        Raises :class:`~second_brain_core.errors.WordSearchUnavailableError` when the
        store has no word index.
        """
        visible = await self._visible_for_search()
        hits = await asyncio.to_thread(self._index.word_search, query, limit * QUERY_OVERFETCH)
        return self._best(hits, visible, limit)
