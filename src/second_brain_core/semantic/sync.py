"""Pure comparison of a source's current items with the indexed versions."""

from .types import SyncPlan


def diff_versions(current: dict[str, str], indexed: dict[str, str]) -> SyncPlan:
    """Which items need (re)embedding and which to drop.

    ``current`` maps item id → version marker for every visible item; ``indexed``
    is the same map for what the index holds. An item is *changed* when it is new or
    its marker differs; *deleted* when it is in the index but no longer visible.
    """
    changed_ids = [
        item_id for item_id, version in current.items() if indexed.get(item_id) != version
    ]
    deleted_ids = [item_id for item_id in indexed if item_id not in current]
    return SyncPlan(changed_ids=changed_ids, deleted_ids=deleted_ids)
