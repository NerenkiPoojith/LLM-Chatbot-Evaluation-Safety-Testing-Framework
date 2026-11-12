"""Text embeddings.

An embedding model turns a piece of text into a list of numbers (a vector) so
that texts with similar meaning end up close to each other. We use it twice:

1. Retrieval: find the manual chunks closest to the user's question.
2. Evaluation: check whether each sentence of an answer is close to something
   in the retrieved context (the "semantic-similarity" groundedness check).

We wrap the `fastembed` library in LangChain's `Embeddings` interface, so any
LangChain vector store can use it. fastembed runs a small ONNX model on the CPU,
needs no GPU and no API key, and downloads the model once (~70 MB) into
`.cache/fastembed`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from langchain_core.embeddings import Embeddings


# fastembed's default batch of 256 texts made the model's attention buffers peak at about 1.2 GB
# while indexing a 240-section manual; batches of 16 peak at about 0.3 GB and are no slower.
BATCH_SIZE = 16


class FastEmbedEmbeddings(Embeddings):
    """LangChain-compatible wrapper around a fastembed model."""

    def __init__(self, model_name: str, cache_dir: Path | None = None) -> None:
        from fastembed import TextEmbedding  # imported lazily: loading the model takes a second

        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir) if cache_dir else None)

    # --- LangChain interface -------------------------------------------------
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._model.embed(list(texts), batch_size=BATCH_SIZE)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()

    # --- Helper used by the evaluator ---------------------------------------
    def embed_array(self, texts: list[str]) -> np.ndarray:
        """Embed texts and return an L2-normalised matrix (one row per text)."""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        matrix = np.array(list(self._model.embed(list(texts), batch_size=BATCH_SIZE)), dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.clip(norms, 1e-12, None)


def cosine_similarity_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity between every row of `a` and every row of `b`.

    Both inputs must already be L2-normalised, so the cosine is a dot product.
    Result shape: (len(a), len(b)), values roughly in [-1, 1].
    """
    if a.size == 0 or b.size == 0:
        return np.zeros((a.shape[0], b.shape[0]), dtype=np.float32)
    return a @ b.T


@lru_cache(maxsize=2)
def get_embeddings(model_name: str, cache_dir: str) -> FastEmbedEmbeddings:
    """Load the model once per process and reuse it everywhere."""
    return FastEmbedEmbeddings(model_name=model_name, cache_dir=Path(cache_dir))
