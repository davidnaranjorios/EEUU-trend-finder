"""Cruces término–serie y término–término por similitud semántica (ADR 0009).

Nunca hay mapeos escritos a mano: el enlace sale del coseno entre embeddings locales del término
y del título de la serie. El umbral también sale de los datos y es **por término**:

    umbral = cuantil `null_quantile` de  max_j cos(v, serie_j)  sobre términos v del vocabulario

donde v recorre una muestra con semilla fija de términos comunes de `term_freq` **sin señal**.
Es decir, una señal se enlaza a una serie solo si se le parece más de lo que la mejor coincidencia
de un término cualquiera se parece a alguna serie (con 0,95: más que el 95 % de los términos
comunes). Un umbral por par (cuantil de cosenos al azar) no sirve con cientos de miles de series:
con 0,999 dejaría ~400 coincidencias por azar por término. Además, cada término guarda a lo sumo
`top_k` series.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class LinkParams:
    null_quantile: float
    null_terms: int
    top_k: int
    seed: int
    chunk: int = 256


def best_match(vecs: np.ndarray, targets: np.ndarray, chunk: int = 256) -> np.ndarray:
    """max_j cos(v_i, target_j) para cada fila de `vecs`."""
    if vecs.shape[0] == 0 or targets.shape[0] == 0:
        return np.zeros(vecs.shape[0])
    return np.concatenate(
        [(vecs[lo : lo + chunk] @ targets.T).max(axis=1) for lo in range(0, vecs.shape[0], chunk)]
    )


def null_threshold(null_vecs: np.ndarray, targets: np.ndarray, params: LinkParams) -> float:
    """Cuantil de la mejor coincidencia de los términos del nulo contra `targets`."""
    if null_vecs.shape[0] == 0 or targets.shape[0] == 0:
        return 1.0
    return float(np.quantile(best_match(null_vecs, targets, params.chunk), params.null_quantile))


def sample_null(candidates: list[str], exclude: set[str], params: LinkParams) -> list[str]:
    """Muestra con semilla fija de términos del vocabulario que no son señales."""
    pool = sorted(set(candidates) - exclude)
    if len(pool) <= params.null_terms:
        return pool
    rng = np.random.default_rng(params.seed)
    return [pool[i] for i in sorted(rng.choice(len(pool), params.null_terms, replace=False))]


def top_links(
    terms: np.ndarray, series: np.ndarray, threshold: float, params: LinkParams
) -> list[tuple[int, int, float]]:
    """(término, serie, coseno) con coseno > umbral (estricto), a lo sumo `top_k` por término."""
    out: list[tuple[int, int, float]] = []
    if terms.shape[0] == 0 or series.shape[0] == 0:
        return out
    for lo in range(0, terms.shape[0], params.chunk):
        sims = terms[lo : lo + params.chunk] @ series.T
        for r, row in enumerate(sims):
            cand = np.flatnonzero(row > threshold)
            if cand.size > params.top_k:
                cand = cand[np.argpartition(-row[cand], params.top_k - 1)[: params.top_k]]
            for j in sorted(cand, key=lambda j: (-row[j], j)):
                out.append((lo + r, int(j), float(row[j])))
    return out
