"""The search engine over a source: sync, the visibility rule at every search, limits."""

import asyncio
import threading
import time

import pytest

from second_brain_core.semantic.engine import SEARCH_CONCURRENCY, Engine
from second_brain_core.semantic.testing import (
    FakeEmbedder,
    FakeItem,
    FakeSource,
    FakeVectorIndex,
    SharedVectorIndex,
    fake_embedding,
)
from second_brain_core.semantic.types import ChunkRecord


def engine(
    source: FakeSource,
    index: FakeVectorIndex | None = None,
    embedder: FakeEmbedder | None = None,
    *,
    auto_sync: bool = True,
) -> tuple[Engine, FakeVectorIndex, FakeEmbedder]:
    index = index if index is not None else FakeVectorIndex()
    embedder = embedder or FakeEmbedder()
    return Engine(source, embedder, index, auto_sync=auto_sync), index, embedder


def three() -> FakeSource:
    return FakeSource(
        items={
            "a": FakeItem("Roof", "the roof leaks over the garage"),
            "b": FakeItem("Garden", "tomatoes and basil in the garden"),
            "c": FakeItem("Empty", "   "),
        }
    )


async def test_a_first_sync_embeds_text_and_marks_the_empty() -> None:
    source = three()
    e, index, _ = engine(source)
    stats = await e.sync()
    assert (stats.added, stats.updated, stats.deleted, stats.total) == (2, 0, 0, 3)
    assert stats.model_name == "fake-model"
    assert index.item_versions() == {"a": "1", "b": "1"}
    assert index.empty_versions() == {"c": "1"}
    assert index.dim == 16


async def test_a_second_sync_reads_nothing_unchanged() -> None:
    source = three()
    e, _, embedder = engine(source)
    await e.sync()
    source.reads.clear()
    stats = await e.sync()
    assert (stats.added, stats.updated, stats.deleted) == (0, 0, 0)
    assert source.reads == [] and embedder.doc_calls == 2


async def test_changes_updates_and_deletions_are_counted() -> None:
    source = three()
    e, index, _ = engine(source)
    await e.sync()
    source.items["a"].version, source.items["a"].text = "2", "the roof is fixed"
    source.items["c"].version, source.items["c"].text = "2", "now it has text"
    del source.items["b"]
    stats = await e.sync()
    # c was a mark: gaining text is an addition, not an update.
    assert (stats.added, stats.updated, stats.deleted, stats.total) == (1, 1, 1, 2)
    assert index.item_versions() == {"a": "2", "c": "2"}
    assert index.empty_versions() == {}


async def test_an_item_emptied_is_an_update_and_becomes_a_mark() -> None:
    source = three()
    e, index, _ = engine(source)
    await e.sync()
    source.items["a"].version, source.items["a"].text = "2", ""
    stats = await e.sync()
    assert stats.updated == 1
    assert "a" not in index.item_versions() and index.empty_versions()["a"] == "2"


async def test_an_item_not_ready_is_not_marked_and_is_read_again() -> None:
    source = three()
    source.items["a"].ready = False
    e, index, _ = engine(source)
    await e.sync()
    assert "a" not in index.item_versions() and "a" not in index.empty_versions()
    source.reads.clear()
    await e.sync()
    assert source.reads == ["a"]


async def test_an_item_hidden_between_listing_and_reading_is_not_kept() -> None:
    source = three()
    source.hide_on_read = {"a"}
    e, index, _ = engine(source)
    stats = await e.sync()
    assert stats.added == 1
    assert "a" not in index.item_versions() and "roof" not in index.stored_text()


async def test_a_source_that_is_down_leaves_the_index_alone() -> None:
    source = three()
    e, index, _ = engine(source)
    await e.sync()
    before = (index.item_versions(), index.empty_versions(), index.stored_text())
    source.down = True
    for full in (False, True):
        with pytest.raises(ConnectionError):
            await e.sync(full=full)
    assert (index.item_versions(), index.empty_versions(), index.stored_text()) == before


async def test_a_new_model_or_width_rebuilds() -> None:
    source = three()
    index = FakeVectorIndex()
    e, _, _ = engine(source, index)
    await e.sync()
    again, _, embedder = engine(source, index, FakeEmbedder("another-model"))
    stats = await again.sync()
    assert stats.added == 2 and index.model_name == "another-model"
    wider, _, _ = engine(source, index, FakeEmbedder("another-model", dim=24))
    stats = await wider.sync()
    assert stats.added == 2 and index.dim == 24


async def test_full_rebuilds_everything() -> None:
    source = three()
    e, _, embedder = engine(source)
    await e.sync()
    stats = await e.sync(full=True)
    assert stats.added == 2 and embedder.doc_calls == 4


async def test_prune_drops_what_is_gone_and_embeds_nothing() -> None:
    source = three()
    e, index, embedder = engine(source)
    await e.sync()
    source.items["a"].visible = False
    del source.items["c"]
    assert await e.prune() == 1
    assert index.item_versions() == {"b": "1"} and index.empty_versions() == {}
    assert embedder.doc_calls == 2


# -- search -----------------------------------------------------------------------


async def test_search_returns_each_items_best_chunk_best_first() -> None:
    long_text = "\n\n".join(f"paragraph {i} " + "word " * 300 for i in range(3)) + "\n\nroof"
    source = FakeSource(
        items={"long": FakeItem("Long", long_text), "roof": FakeItem("Roof", "roof leaks")}
    )
    e, _, _ = engine(source)
    hits = await e.search("roof leaks", limit=5)
    assert [hit.item_id for hit in hits][0] == "roof"
    assert len({hit.item_id for hit in hits}) == len(hits)
    assert hits == sorted(hits, key=lambda hit: hit.score, reverse=True)
    assert len(await e.search("roof leaks", limit=1)) == 1


async def test_a_hidden_item_never_reaches_a_result() -> None:
    """Including one hidden after it was indexed, with auto-sync off."""
    source = three()
    source.filter_active = True
    e, index, _ = engine(source, auto_sync=False)
    await e.sync()
    source.items["a"].visible = False
    hits = await e.search("roof leaks garage")
    assert "a" not in {hit.item_id for hit in hits}
    assert "a" not in index.item_versions()  # and the index was tidied
    words = await e.word_search("roof")
    assert words == []


async def test_an_intruder_in_a_shared_index_is_dropped() -> None:
    """Another process wrote an item this source hides into the same index file."""
    source = three()
    source.filter_active = True
    index = SharedVectorIndex()
    index.intruders = [
        ChunkRecord(
            chunk_id="secret::0",
            item_id="secret",
            title="Secret",
            version="1",
            chunk_index=0,
            text="roof secret",
            embedding=fake_embedding("roof secret"),
        )
    ]
    e, _, _ = engine(source, index)
    assert "secret" not in {hit.item_id for hit in await e.search("roof secret")}
    assert "secret" not in {hit.item_id for hit in await e.word_search("secret")}


async def test_without_a_filter_a_search_without_auto_sync_lists_nothing() -> None:
    source = three()
    e, _, _ = engine(source, auto_sync=False)
    await e.sync()
    listings = source.listings
    await e.search("roof")
    assert source.listings == listings


async def test_with_auto_sync_a_search_syncs_first() -> None:
    source = three()
    e, index, _ = engine(source)
    hits = await e.search("garden basil")
    assert hits and hits[0].item_id == "b"
    assert index.item_versions() == {"a": "1", "b": "1"}


async def test_word_search_finds_every_word_ignoring_case_and_accents() -> None:
    source = FakeSource(items={"r": FakeItem("Réunion", "la RÉUNION du comité"), **three().items})
    e, _, _ = engine(source)
    assert [hit.item_id for hit in await e.word_search("reunion comite")] == ["r"]
    assert await e.word_search("reunion garage") == []
    assert await e.word_search("  ,;  ") == []


async def test_searches_are_capped() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    class SlowEmbedder(FakeEmbedder):
        def embed_query(self, text: str) -> list[float]:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return super().embed_query(text)

    e, _, _ = engine(three(), embedder=SlowEmbedder(), auto_sync=False)
    await asyncio.gather(*(e.search("q") for _ in range(6)))
    assert peak == SEARCH_CONCURRENCY
