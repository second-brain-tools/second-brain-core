"""The embedders, backed by the optional extras.

Coverage-omitted: they need a model download, so CI doesn't run them. They are
type-checked by mypy (the optional libraries resolve to ``Any``).
"""

from __future__ import annotations

from ..errors import SemanticUnavailableError
from ..server import Server
from .stores import install_hint
from .types import Embedder

BACKENDS = ("fastembed", "sentence-transformers")
FASTEMBED_DEFAULT = "BAAI/bge-small-en-v1.5"
SENTENCE_TRANSFORMERS_DEFAULT = "BAAI/bge-m3"


def build_embedder(server: Server, backend: str, model: str = "") -> Embedder:
    """The configured embedder (``model`` empty: the backend's default), or a clear refusal."""
    if backend == "fastembed":
        return _FastEmbedEmbedder(server, model or FASTEMBED_DEFAULT)
    if backend == "sentence-transformers":
        return _SentenceTransformerEmbedder(server, model or SENTENCE_TRANSFORMERS_DEFAULT)
    raise SemanticUnavailableError(
        f"Unknown {server.var('EMBEDDING_BACKEND')} {backend!r}; expected 'fastembed' or "
        "'sentence-transformers'."
    )


class _FastEmbedEmbedder:
    def __init__(self, server: Server, model_name: str) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise SemanticUnavailableError(install_hint(server, "fastembed")) from exc
        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name)
        probe = next(iter(self._model.embed(["probe"])))
        self.dim = len(probe)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in vec] for vec in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        vec = next(iter(self._model.query_embed([text])))
        return [float(x) for x in vec]


class _SentenceTransformerEmbedder:
    def __init__(self, server: Server, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise SemanticUnavailableError(
                install_hint(server, "sentence-transformers", extra="semantic-st")
            ) from exc
        self.model_name = model_name
        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return [[float(x) for x in vec] for vec in vecs]

    def embed_query(self, text: str) -> list[float]:
        vec = self._model.encode([text], normalize_embeddings=True)[0]
        return [float(x) for x in vec]
