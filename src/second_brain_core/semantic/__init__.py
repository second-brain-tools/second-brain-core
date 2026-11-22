"""Search by meaning (and by words) over a server's items: the ``semantic`` extra.

A server supplies a :class:`~.types.Source` (its items, their versions, which are
visible) and gets an :class:`~.engine.Engine` that keeps an index of them in step
and searches it. Nothing here imports an optional dependency until a store or an
embedder is built (:mod:`.stores`, :mod:`.embedders`).
"""
