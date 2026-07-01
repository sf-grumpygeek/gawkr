from __future__ import annotations

import logging

from fastembed import TextEmbedding

log = logging.getLogger("gawkr.embed")

# bge-small-en-v1.5 -> 384 dims. If you change the model, update EMBED_DIM in store.py.
EMBED_DIM = 384


class Embedder:
    def __init__(self, model_name: str):
        log.info("loading embedding model %s", model_name)
        self._model = TextEmbedding(model_name=model_name)

    def embed(self, text: str) -> list[float]:
        # fastembed is CPU/ONNX and returns a generator of numpy arrays.
        vec = next(iter(self._model.embed([text or ""])))
        return [float(x) for x in vec]
