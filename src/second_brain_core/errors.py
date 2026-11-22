"""The errors the shared pieces raise."""


class ServeConfigError(Exception):
    """A server was asked for a configuration it refuses to start with."""


class FilterError(Exception):
    """A server's filter setting names something its store doesn't have.

    The tools it governs refuse while this holds: a filter that can't be resolved
    must not fall back to showing everything. ``str()`` is the same fixed sentence
    every time (``MESSAGE``) and is safe to hand to a remote client. ``detail`` says
    which variable and entry is wrong; an entry can be the name of something the
    operator wants hidden, so the detail is for the operator only (the log, the
    command line). Each server subclasses it, setting ``MESSAGE`` and ``WHAT``.
    """

    MESSAGE = "The server's filter settings don't match its store. Its log says which."
    WHAT = "filter"  # what the log line calls it: "the notebook filter doesn't resolve"

    def __init__(self, detail: str) -> None:
        super().__init__(self.MESSAGE)
        self.detail = detail


class SemanticUnavailableError(Exception):
    """Search by meaning can't run: an optional dependency is missing, or the index
    can't be used (it belongs to another server, say). The message says what to do."""


class IndexOwnedElsewhereError(SemanticUnavailableError):
    """The index directory holds another server's index: each server needs its own."""


class WordSearchUnavailableError(SemanticUnavailableError):
    """The index has no word search: the Chroma store, or a SQLite without FTS5."""
