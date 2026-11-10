"""Embeddings: text -> a unit vector stored as float32 bytes, so meaning is one more thing SQL can rank by.

    embed('where do I live')            SQL function, the query side
    cosine(a, b)                        SQL function, similarity of two stored vectors

The model is a small local one (config.EMBED_MODEL, run by fastembed on the CPU). With no model
configured, a hashed bag of words stands in: enough for tests and for a host without the model.
"""
from __future__ import annotations

import functools
import logging
import re
import zlib

import numpy as np

from . import config

log = logging.getLogger("vault-embed")
HASH_DIM = 256


@functools.lru_cache(maxsize=2)
def _model(name: str):
    from fastembed import TextEmbedding  # imported late: the hash fallback needs none of it
    return TextEmbedding(name, cache_dir=str(config.CACHE_ROOT / "models"))


def model_id() -> str:
    return config.EMBED_MODEL or f"hash-{HASH_DIM}"


def _hashed(text: str) -> np.ndarray:
    v = np.zeros(HASH_DIM, dtype=np.float32)
    for word in re.findall(r"\w+", text.lower()):
        v[zlib.crc32(word.encode()) % HASH_DIM] += 1.0
    return v


def _unit(v: np.ndarray) -> bytes:
    norm = float(np.linalg.norm(v))
    return (v / norm if norm else v).astype(np.float32).tobytes()


def passages(texts: list[str]) -> list[bytes]:
    """Vectors for stored text."""
    if not texts:
        return []
    if not config.EMBED_MODEL:
        return [_unit(_hashed(t)) for t in texts]
    return [_unit(np.asarray(v)) for v in _model(config.EMBED_MODEL).passage_embed(texts)]


@functools.lru_cache(maxsize=256)
def _query(model: str, text: str) -> bytes:
    if not model:
        return _unit(_hashed(text))
    return _unit(np.asarray(next(iter(_model(model).query_embed(text)))))


def query(text: str | None) -> bytes | None:
    """The vector of a question (SQL: embed(text))."""
    return _query(config.EMBED_MODEL, text) if text else None


def cosine(a: bytes | None, b: bytes | None) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    return float(np.dot(np.frombuffer(a, dtype=np.float32), np.frombuffer(b, dtype=np.float32)))


def nearest(vector: bytes | None, rows: list, k: int) -> list[int]:
    """Ids of the k stored vectors closest to `vector`; rows are (id, embedding)."""
    rows = [r for r in rows if vector and r[1] and len(r[1]) == len(vector)]
    if not rows:
        return []
    matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    scores = matrix @ np.frombuffer(vector, dtype=np.float32)
    return [rows[i][0] for i in np.argsort(-scores)[:k]]
