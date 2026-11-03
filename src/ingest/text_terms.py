"""N-gramas por documento y frecuencias de documentos por periodo (ADR 0005).

El texto completo no se guarda (decisión del 2026-11-03): cada sección se normaliza, se le
calcula `text_hash` y el conjunto de n-gramas (1..n) que contiene, representado como hashes de
64 bits. Al cerrar un periodo se cuentan los documentos por hash, se conservan los que llegan a
`min_doc_count` y solo para esos se recupera el texto del n-grama releyendo una copia temporal
local de las secciones, que se borra al terminar el periodo.

Sin listas de palabras: el único filtro es el umbral de documentos. Las palabras funcionales
("stopwords") se filtran en el detector (parte B) con reglas sobre los datos de todos los
periodos a la vez, para no crear saltos artificiales entre periodos.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

import numpy as np

# Un n-grama no cruza puntuación de fin de frase ni de cláusula.
FRAGMENT_SPLIT_RE = re.compile(r"[.;:!?()\[\]{}•·|\n\r\t]+")
TOKEN_RE = re.compile(r"[a-z]+")
SPACE_RE = re.compile(r"[   -​　]+")


def normalize(text: str) -> str:
    """NFKC, minúsculas, espacios colapsados por línea y sin líneas vacías."""
    text = unicodedata.normalize("NFKC", text).lower()
    lines = (SPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def text_hash(normalized: str) -> str:
    """sha256 del texto normalizado (permite detectar duplicados sin guardar el texto)."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def iter_ngrams(normalized: str, n_max: int) -> Iterator[str]:
    """Todos los n-gramas (con repetición) de 1 a `n_max` tokens alfabéticos."""
    for fragment in FRAGMENT_SPLIT_RE.split(normalized):
        tokens = TOKEN_RE.findall(fragment)
        for n in range(1, n_max + 1):
            for i in range(len(tokens) - n + 1):
                yield " ".join(tokens[i : i + n])


def hash_ngram(gram: str) -> int:
    """Hash estable de 64 bits (no depende de PYTHONHASHSEED ni del proceso)."""
    return int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "little")


def ngram_hashes(normalized: str, n_max: int) -> np.ndarray:
    """Hashes únicos (ordenados) de los n-gramas de un documento."""
    grams = set(iter_ngrams(normalized, n_max))
    out = np.fromiter((hash_ngram(g) for g in grams), dtype=np.uint64, count=len(grams))
    out.sort()
    return out


def write_temp_text(path: Path, normalized: str) -> None:
    """Copia temporal local (no se sincroniza a R2) para recuperar el texto de los n-gramas."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=1) as fh:
        fh.write(normalized)


def read_temp_text(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


class PeriodCounter:
    """Cuenta, por sección, en cuántos documentos aparece cada hash de n-grama."""

    def __init__(self) -> None:
        self._chunks: dict[str, list[np.ndarray]] = defaultdict(list)
        self.total_docs: dict[str, int] = defaultdict(int)

    def add(self, section: str, hashes: np.ndarray) -> None:
        self._chunks[section].append(hashes)
        self.total_docs[section] += 1

    def sections(self) -> list[str]:
        return sorted(self.total_docs)

    def retained(self, section: str, min_doc_count: int) -> tuple[np.ndarray, np.ndarray]:
        """(hashes, doc_count) de los n-gramas con al menos `min_doc_count` documentos."""
        chunks = self._chunks.get(section) or [np.empty(0, dtype=np.uint64)]
        values, counts = np.unique(np.concatenate(chunks), return_counts=True)
        keep = counts >= min_doc_count
        return values[keep], counts[keep].astype(np.int64)


def resolve_strings(wanted: np.ndarray, texts: Iterable[str], n_max: int) -> dict[int, str]:
    """Texto de cada hash de `wanted`, releyendo documentos hasta encontrarlos todos."""
    wanted_set = set(int(h) for h in wanted)
    found: dict[int, str] = {}
    for text in texts:
        for gram in set(iter_ngrams(text, n_max)):
            h = hash_ngram(gram)
            if h in wanted_set and h not in found:
                found[h] = gram
        if len(found) == len(wanted_set):
            break
    return found
