"""Embeddings: text -> a unit vector stored as float32 bytes, so meaning is one more thing SQL can rank by.

    embed('where do I live')            SQL function, the query side
    cosine(a, b)                        SQL function, similarity of two stored vectors

The model is an ONNX export run on the CPU by onnxruntime (config.EMBED_MODEL, a Hugging Face repository;
by default Microsoft's Harrier OSS v1 0.6B, multilingual, 1,024 dimensions). Harrier is trained with an
instruction on the query side and none on the passage side. With no model configured, a hashed bag of
words stands in: enough for tests and for a host without the model.
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


@functools.lru_cache(maxsize=1)
def _model(repo: str, file: str):
    """(tokenizer, session, input names, output name) of the ONNX model, downloaded once into the cache."""
    import onnxruntime as ort  # imported late: the hash fallback needs none of it
    from huggingface_hub import snapshot_download
    from tokenizers import Tokenizer
    ort.disable_telemetry_events()
    root = config.CACHE_ROOT / "models" / repo.replace("/", "--")
    if not ((root / file).is_file() and (root / "tokenizer.json").is_file()):  # downloaded once, then offline
        snapshot_download(  # real files, not cache symlinks: onnxruntime wants the weights beside the graph
            repo, allow_patterns=[file, f"{file}_data*", "tokenizer.json"], local_dir=str(root))
    tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=config.EMBED_MAX_TOKENS)
    tokenizer.no_padding()
    options = ort.SessionOptions()
    options.intra_op_num_threads = config.EMBED_THREADS
    session = ort.InferenceSession(str(root / file), options, providers=["CPUExecutionProvider"])
    outputs = [o.name for o in session.get_outputs()]
    output = "sentence_embedding" if "sentence_embedding" in outputs else outputs[0]
    log.info("embedding model %s/%s loaded (output %s)", repo, file, output)
    return tokenizer, session, {i.name for i in session.get_inputs()}, output


def model_id() -> str:
    return f"{config.EMBED_MODEL}/{config.EMBED_FILE}" if config.EMBED_MODEL else f"hash-{HASH_DIM}"


def _hashed(text: str) -> np.ndarray:
    v = np.zeros(HASH_DIM, dtype=np.float32)
    for word in re.findall(r"\w+", text.lower()):
        v[zlib.crc32(word.encode()) % HASH_DIM] += 1.0
    return v


def _unit(v: np.ndarray) -> bytes:
    v = np.asarray(v, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    return (v / norm if norm else v).tobytes()


def _encode(text: str) -> bytes:
    """One text at a time: no padding, so the last token is the one the model pools."""
    if not config.EMBED_MODEL:
        return _unit(_hashed(text))
    tokenizer, session, inputs, output = _model(config.EMBED_MODEL, config.EMBED_FILE)
    ids = np.asarray([tokenizer.encode(text).ids], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
    if "position_ids" in inputs:
        feed["position_ids"] = np.arange(ids.shape[1], dtype=np.int64)[None, :]
    out = session.run([output], {k: v for k, v in feed.items() if k in inputs})[0]
    return _unit(out[0] if out.ndim == 2 else out[0, -1])  # (1, dim) pooled, or (1, tokens, dim): last token


def passage(title: str | None, text: str) -> bytes:
    """The vector of stored text, with the title of its page for context."""
    return _encode(f"{title}: {text}" if title else text)


@functools.lru_cache(maxsize=256)
def _query(model: str, text: str) -> bytes:
    return _encode((config.EMBED_QUERY_PREFIX if config.EMBED_MODEL else "") + text)


def query(text: str | None) -> bytes | None:
    """The vector of a question (SQL: embed(text))."""
    return _query(model_id(), text) if text else None


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
