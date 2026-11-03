"""Embeddings locales de texto corto (títulos de series y términos), con caché en Parquet.

Backends (`rank.embeddings.backend`):
- `fastembed`: `fastembed` (Apache-2.0, ONNX, sin torch) con el modelo de `rank.embeddings.model`
  (por defecto `BAAI/bge-small-en-v1.5`, MIT). Se instala con `uv sync --group embed`.
- `hashing`: bolsa de n-gramas de caracteres con hashing, determinista y sin red. Sirve para los
  tests y como respaldo; su similitud es léxica, no semántica.

Todos devuelven vectores de norma 1 (el producto punto es el coseno). La caché guarda un vector
por (modelo, sha1 del texto) en `processed/_internal/embeddings/<modelo>/NN/`: una corrida
nueva solo calcula los textos que no están.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pyarrow as pa

from src.ingest.store import read_table, upsert
from src.schemas import TableSpec

EMBEDDINGS = TableSpec(
    name="embeddings",
    schema=pa.schema(
        [
            pa.field("model", pa.string(), nullable=False),
            pa.field("text_hash", pa.string(), nullable=False),
            pa.field("vector", pa.list_(pa.float32()), nullable=False),
        ]
    ),
    key=("model", "text_hash"),
)
CACHE_BUCKETS = 16
_TOKEN = re.compile(r"[0-9a-z]+")


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return (m / np.where(norms > 0, norms, 1.0)).astype(np.float32)


class HashingEmbedder:
    """Trigramas de caracteres por palabra más palabras completas, con hashing con signo."""

    def __init__(self, dim: int = 4096) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in _TOKEN.findall(text.casefold()):
                feats = [f"w:{tok}"] + [f"c:{tok[j : j + 3]}" for j in range(max(len(tok) - 2, 1))]
                for f in feats:
                    h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
                    sign = 1.0 if (h >> 63) & 1 else -1.0  # hashing con signo: colisiones ~ 0
                    out[i, h % self.dim] += sign * (1.0 if f.startswith("w:") else 0.5)
        return _normalize(out)


class FastEmbedder:
    """Modelo ONNX local vía fastembed (se descarga una vez a `cache_dir`)."""

    def __init__(self, model: str, cache_dir: Path | None, batch_size: int, threads: int) -> None:
        from fastembed import TextEmbedding  # dependencia del grupo `embed`

        self.name = model
        self.batch_size = batch_size
        self._model = TextEmbedding(
            model_name=model,
            cache_dir=str(cache_dir) if cache_dir else None,
            threads=threads or None,
        )

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        vecs = list(self._model.embed(list(texts), batch_size=self.batch_size))
        return _normalize(np.vstack(vecs))


def make_embedder(cfg: dict[str, Any], cache_dir: Path | None = None) -> Embedder:
    e = cfg["rank"]["embeddings"]
    if e["backend"] == "hashing":
        return HashingEmbedder(e.get("hashing_dim", 4096))
    if e["backend"] == "fastembed":
        return FastEmbedder(e["model"], cache_dir, e["batch_size"], e.get("threads", 0))
    raise ValueError(f"rank.embeddings.backend desconocido: {e['backend']!r}")


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def model_slug(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", name)


class CachedEmbedder:
    """Envuelve un embedder con la caché Parquet (idempotente)."""

    def __init__(self, inner: Embedder, root: Path | None) -> None:
        self.inner = inner
        self.name = inner.name
        self.root = root
        self.computed = 0
        self.reused = 0
        self._mem: dict[str, np.ndarray] | None = None

    def _load(self) -> dict[str, np.ndarray]:
        """Vectores ya calculados de este modelo (se lee la caché una vez por instancia)."""
        if self._mem is None:
            self._mem = {}
            if self.root is not None:
                table = read_table(EMBEDDINGS, self.root, source=model_slug(self.name))
                if table.num_rows:
                    vecs = np.asarray(
                        table.column("vector").combine_chunks().flatten(), dtype=np.float32
                    ).reshape(table.num_rows, -1)
                    self._mem = dict(zip(table.column("text_hash").to_pylist(), vecs, strict=True))
        return self._mem

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        uniq = sorted(set(texts))
        hashes = {t: text_hash(t) for t in uniq}
        cached = self._load()
        part = model_slug(self.name)
        missing = [t for t in uniq if hashes[t] not in cached]
        if missing:
            fresh = self.inner.embed(missing)
            for t, v in zip(missing, fresh, strict=True):
                cached[hashes[t]] = v
            if self.root is not None:
                rows = pa.table(
                    {
                        "model": [self.name] * len(missing),
                        "text_hash": [hashes[t] for t in missing],
                        "vector": [v.tolist() for v in fresh],
                    },
                    schema=EMBEDDINGS.schema,
                )
                upsert(
                    rows,
                    EMBEDDINGS,
                    self.root,
                    source=part,
                    n_buckets=CACHE_BUCKETS,
                    bucket_col="text_hash",
                )
        self.computed += len(missing)
        self.reused += len(uniq) - len(missing)
        if not texts:
            return np.zeros((0, 1), dtype=np.float32)
        return np.vstack([cached[hashes[t]] for t in texts]).astype(np.float32)
