from functools import cache

import numpy as np
from fastembed import TextEmbedding

from xoult.config import settings
from xoult.lexical import document_text  # noqa: F401 (re-exported for ingest and evals)


@cache
def _model(name: str) -> TextEmbedding:
    return TextEmbedding(name)


def embed(
    texts: list[str], model: str = settings.embedding_model, parallel: int | None = None
) -> np.ndarray:
    # Attention memory is batch * heads * seq_len^2: at 512 tokens a batch of 64 needs
    # ~800 MB per worker, so keep batches small. `parallel` (worker processes) only pays
    # off for bulk jobs like the eval, not for a day's worth of papers.
    vectors = np.array(
        list(_model(model).embed(texts, batch_size=16, parallel=parallel)), dtype=np.float32
    )
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
