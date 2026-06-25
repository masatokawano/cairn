"""Local sentence-transformers provider for Cairn (P2-1b).

The default is `intfloat/multilingual-e5-small` (384 dim) — small enough to
fit a personal archive's CPU budget and explicitly multilingual, so Japanese
and English passages share an embedding space.

e5 models require role-specific prefixes ('passage: ' for indexed text,
'query: ' for searches); this provider applies them automatically so callers
in db.py stay model-agnostic.

`sentence-transformers` is imported lazily inside the model loader: importing
this module never pulls torch, and tests that swap in a fixture provider can
run without the heavy dependency installed.
"""
from __future__ import annotations

from . import EmbeddingProvider, vector_to_bytes


DEFAULT_MODEL = "intfloat/multilingual-e5-small"
_DIMENSIONS = {
    "intfloat/multilingual-e5-small": 384,
    "intfloat/multilingual-e5-base": 768,
    "intfloat/multilingual-e5-large": 1024,
}


class LocalSbertProvider(EmbeddingProvider):
    """sentence-transformers backed provider. Model loads on first embed call."""

    def __init__(self, model: str = DEFAULT_MODEL, dimension: int | None = None):
        self._model_id = model
        # Allow callers to override for models we don't have hard-coded; for
        # known e5 sizes we fall back to the table so tests / smoke checks
        # don't need to load the model just to learn its width.
        self._dimension = dimension if dimension is not None else _DIMENSIONS.get(model, 0)
        self._model = None  # loaded lazily on first embed call

    @property
    def name(self) -> str:
        return "local-sbert"

    @property
    def model(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        if not self._dimension:
            # Force a load to discover the dimension when the user supplied
            # a model not in our static table.
            self._ensure_loaded()
            self._dimension = self._model.get_sentence_embedding_dimension()
        return self._dimension

    def _ensure_loaded(self) -> None:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is not installed. Install it with "
                    "`pip install sentence-transformers` (or switch to another "
                    "EmbeddingProvider) before calling embed_passages/embed_query."
                ) from exc
            self._model = SentenceTransformer(self._model_id)

    def embed_passages(self, texts: list[str]) -> list[bytes]:
        if not texts:
            return []
        self._ensure_loaded()
        prefixed = [f"passage: {t}" for t in texts]
        # normalize so cosine == dot, matching the e5 paper's recipe
        vectors = self._model.encode(prefixed, normalize_embeddings=True, show_progress_bar=False)
        return [vector_to_bytes(v) for v in vectors]

    def embed_query(self, text: str) -> bytes:
        self._ensure_loaded()
        vector = self._model.encode(
            [f"query: {text}"], normalize_embeddings=True, show_progress_bar=False
        )[0]
        return vector_to_bytes(vector)
