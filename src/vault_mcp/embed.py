"""Embeddings: text -> unit float32 & int8 scalar-quantized bytes for vectors.db & SQL UDFs."""
from __future__ import annotations

import functools
import logging
import re
import struct
import threading
import zlib

import numpy as np

from . import config

log = logging.getLogger("vault-embed")
HASH_DIM = 256
INT8_HEADER = struct.Struct("<f")
_MODEL_LOCK = threading.Lock()


@functools.lru_cache(maxsize=1)
def _model(repo: str, file: str):
    """Load tokenizer and ONNX session into cache once (downloaded to CACHE_ROOT if absent)."""
    import onnxruntime as ort
    from huggingface_hub import snapshot_download
    from tokenizers import Tokenizer

    ort.disable_telemetry_events()
    root = config.CACHE_ROOT / "models" / repo.replace("/", "--")
    if not ((root / file).is_file() and (root / "tokenizer.json").is_file()):
        snapshot_download(
            repo,
            allow_patterns=[file, f"{file}_data*", "tokenizer.json"],
            local_dir=str(root),
        )
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
    """Identifier stored beside each cached vector row."""
    return f"{config.EMBED_MODEL}/{config.EMBED_FILE}" if config.EMBED_MODEL else f"hash-{HASH_DIM}"


def _unit_bytes(values) -> bytes:
    v = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    return (v / norm if norm else v).tobytes()


def _hashed(text: str) -> bytes:
    vec = np.zeros(HASH_DIM, dtype=np.float32)
    for word in re.findall(r"[a-z0-9_]+", text.lower()):
        vec[zlib.crc32(word.encode("utf-8")) % HASH_DIM] += 1.0
    return _unit_bytes(vec)


def _encode(text: str) -> bytes:
    if not config.EMBED_MODEL:
        return _hashed(text)
    with _MODEL_LOCK:
        tokenizer, session, inputs, output = _model(config.EMBED_MODEL, config.EMBED_FILE)
        ids = np.asarray([tokenizer.encode(text).ids], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": np.ones_like(ids)}
        if "position_ids" in inputs:
            feed["position_ids"] = np.arange(ids.shape[1], dtype=np.int64)[None, :]
        out = session.run([output], {k: v for k, v in feed.items() if k in inputs})[0]
        return _unit_bytes(out[0] if out.ndim == 2 else out[0, -1])


def quantize_int8(f32_blob: bytes) -> bytes:
    """Scalar-quantize a float32 vector blob into `<f` scale + signed int8 payload."""
    if not f32_blob or len(f32_blob) % 4 != 0:
        raise ValueError("f32_blob must be a non-empty multiple of 4 bytes")
    vec = np.frombuffer(f32_blob, dtype=np.float32)
    peak = float(np.max(np.abs(vec)))
    scale = peak / 127.0 if peak > 0.0 else 1.0
    q = np.rint(vec / scale).clip(-127, 127).astype(np.int8)
    return INT8_HEADER.pack(scale) + q.tobytes()


def passage(title: str | None, text: str) -> bytes:
    """Embed stored item/episode text as a float32 unit vector."""
    return _encode(f"{title}: {text}" if title else text)


def passage_int8(title: str | None, text: str) -> bytes:
    """Embed stored item/episode text and quantize to int8 (`<f` scale + int8 payload)."""
    return quantize_int8(passage(title, text))


@functools.lru_cache(maxsize=256)
def _cached_query(active_model: str, text: str) -> bytes:
    prefix = config.EMBED_QUERY_PREFIX if config.EMBED_MODEL else ""
    return _encode(prefix + text)


def query(text: str | None) -> bytes | None:
    """Embed a query string as float32 unit bytes (also backing SQL `embed(text)` UDF)."""
    return _cached_query(model_id(), text) if text else None


def cosine_int8(q_f32_blob: bytes | None, q8_blob: bytes | None) -> float | None:
    """Asymmetric cosine similarity between a float32 query vector and an int8 stored blob."""
    if not q_f32_blob or not q8_blob or len(q8_blob) <= INT8_HEADER.size:
        return None
    dim = len(q8_blob) - INT8_HEADER.size
    if len(q_f32_blob) != dim * 4:
        return None
    (scale,) = INT8_HEADER.unpack_from(q8_blob, 0)
    qv = np.frombuffer(q_f32_blob, dtype=np.float32)
    q8 = np.frombuffer(q8_blob, dtype=np.int8, offset=INT8_HEADER.size).astype(np.float32)
    return float(scale * np.dot(qv, q8))


def cosine(a: bytes | None, b: bytes | None) -> float | None:
    """Cosine similarity supporting float32/float32 and float32/int8 pairs (SQL `cosine` UDF)."""
    if not a or not b:
        return None
    hdr = INT8_HEADER.size
    if len(a) % 4 == 0 and len(b) == hdr + len(a) // 4:
        return cosine_int8(a, b)
    if len(b) % 4 == 0 and len(a) == hdr + len(b) // 4:
        return cosine_int8(b, a)
    if len(a) != len(b) or len(a) % 4 != 0:
        return None
    va = np.frombuffer(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    return float(np.dot(va, vb))


def nearest(vector: bytes | None, rows: list[tuple[object, bytes]], k: int) -> list[tuple[object, float]]:
    """Return top-k `(id, score)` sorted descending from float32 `(id, embedding)` pairs."""
    if not vector or k <= 0:
        return []
    dim_bytes = len(vector)
    valid = [(row_id, blob) for row_id, blob in rows if blob and len(blob) == dim_bytes]
    if not valid:
        return []
    matrix = np.frombuffer(b"".join(b for _, b in valid), dtype=np.float32).reshape(len(valid), -1)
    scores = matrix @ np.frombuffer(vector, dtype=np.float32)
    top_idx = np.argsort(-scores)[:k]
    return [(valid[i][0], float(scores[i])) for i in top_idx]


def nearest_int8(vector: bytes | None, rows: list[tuple[object, bytes]], k: int) -> list[tuple[object, float]]:
    """Return top-k `(id, score)` from float32 query vector and int8 `(id, embedding)` pairs."""
    if not vector or k <= 0 or len(vector) % 4 != 0:
        return []
    dim = len(vector) // 4
    expected_len = INT8_HEADER.size + dim
    valid = [(row_id, blob) for row_id, blob in rows if blob and len(blob) == expected_len]
    if not valid:
        return []
    qv = np.frombuffer(vector, dtype=np.float32)
    scales = np.fromiter((INT8_HEADER.unpack_from(b, 0)[0] for _, b in valid), dtype=np.float32, count=len(valid))
    matrix = np.frombuffer(
        b"".join(b[INT8_HEADER.size:] for _, b in valid), dtype=np.int8
    ).reshape(len(valid), dim).astype(np.float32)
    scores = (matrix @ qv) * scales
    top_idx = np.argsort(-scores)[:k]
    return [(valid[i][0], float(scores[i])) for i in top_idx]
