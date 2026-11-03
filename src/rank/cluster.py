"""Deduplicación en clusters (ADR 0009).

Similitud entre dos series con señal:

    s = α · max(ρ, 0) + (1 − α) · cos_título

- ρ: correlación de Pearson de las tasas de crecimiento preparadas igual que en los detectores
  (log-crecimiento, desestacionalización de NSA, winsorización), sobre la grilla más gruesa de las
  dos (los crecimientos mensuales se suman por trimestre o año completo). Con menos de
  `min_overlap[grilla]` periodos en común, ρ = 0.
- cos_título: coseno entre embeddings de "título | unidades".

Los pares con ρ ≥ `duplicate_rho` y cos ≥ `duplicate_cos` son copias del mismo dato (p. ej. la
misma serie en FRED y en BLS) y se fuerzan a s = 1. Después, clustering jerárquico de enlace
promedio cortado en s ≥ `threshold` (el enlace promedio no encadena como las componentes conexas).
Si hay más de `max_series` series, las de menor q entran al clustering y el resto queda como
clusters de una serie (se cuenta en `rank_runs`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

GRID_ORDER = {"M": 0, "Q": 1, "A": 2}
# Periodos finos por periodo grueso.
_RATIO = {("M", "Q"): 3, ("M", "A"): 12, ("Q", "A"): 4}


@dataclass
class GrowthSeries:
    """Crecimiento preparado de una serie, indexado en su grilla (último índice = last_index)."""

    growth: np.ndarray
    last_index: int
    grid: str


@dataclass
class ClusterParams:
    alpha: float
    threshold: float
    duplicate_rho: float
    duplicate_cos: float
    min_overlap: dict[str, int]
    max_series: int


def coarsen(g: GrowthSeries, target: str) -> dict[int, float]:
    """Crecimientos en la grilla `target`, sumando solo periodos gruesos completos."""
    first = g.last_index - g.growth.size + 1
    if target == g.grid:
        return {first + i: float(v) for i, v in enumerate(g.growth)}
    r = _RATIO[(g.grid, target)]
    out: dict[int, list[float]] = {}
    for i, v in enumerate(g.growth):
        out.setdefault((first + i) // r, []).append(float(v))
    return {k: float(sum(v)) for k, v in out.items() if len(v) == r}


def growth_correlation(series: Sequence[GrowthSeries], min_overlap: dict[str, int]) -> np.ndarray:
    """Matriz ρ (n × n) con observaciones comunes por par, en la grilla más gruesa del par."""
    n = len(series)
    rho = np.zeros((n, n))
    order = np.array([GRID_ORDER[s.grid] for s in series])
    for target, level in GRID_ORDER.items():
        idx = np.flatnonzero(order <= level)
        if idx.size < 2:
            continue
        maps = [coarsen(series[i], target) for i in idx]
        periods = sorted(set().union(*maps))
        if not periods:
            continue
        pos = {p: j for j, p in enumerate(periods)}
        x = np.zeros((idx.size, len(periods)))
        m = np.zeros_like(x)
        for r, mp in enumerate(maps):
            for p, v in mp.items():
                x[r, pos[p]] = v
                m[r, pos[p]] = 1.0
        c = _masked_corr(x, m, min_overlap[target])
        # Solo los pares cuya grilla más gruesa es `target`.
        pair_level = np.maximum.outer(order[idx], order[idx])
        sel = pair_level == level
        sub = rho[np.ix_(idx, idx)]
        sub[sel] = c[sel]
        rho[np.ix_(idx, idx)] = sub
    np.fill_diagonal(rho, 1.0)
    return rho


def _masked_corr(x: np.ndarray, m: np.ndarray, min_overlap: int) -> np.ndarray:
    """Pearson por pares con observaciones completas (x = 0 donde m = 0)."""
    n = m @ m.T
    sx = x @ m.T  # suma de x_i en el solape con j
    sy = sx.T
    sxx = (x * x) @ m.T
    syy = sxx.T
    sxy = x @ x.T
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = n * sxy - sx * sy
        var = (n * sxx - sx**2) * (n * syy - sy**2)
        c = cov / np.sqrt(var)
    c[~np.isfinite(c) | (n < min_overlap)] = 0.0
    return np.clip(c, -1.0, 1.0)


def similarity(rho: np.ndarray, cos: np.ndarray, p: ClusterParams) -> np.ndarray:
    s = p.alpha * np.maximum(rho, 0.0) + (1 - p.alpha) * np.clip(cos, 0.0, 1.0)
    s[(rho >= p.duplicate_rho) & (cos >= p.duplicate_cos)] = 1.0
    np.fill_diagonal(s, 1.0)
    return np.clip(s, 0.0, 1.0)


def cluster_labels(s: np.ndarray, threshold: float) -> np.ndarray:
    """Etiquetas 0..k-1 del enlace promedio cortado en similitud ≥ threshold."""
    n = s.shape[0]
    if n == 0:
        return np.zeros(0, dtype=int)
    if n == 1:
        return np.zeros(1, dtype=int)
    d = 1.0 - (s + s.T) / 2
    np.fill_diagonal(d, 0.0)
    z = linkage(squareform(np.clip(d, 0.0, 1.0), checks=False), method="average")
    labels = fcluster(z, t=1.0 - threshold, criterion="distance")
    # Renumerar en orden de primera aparición (estable).
    seen: dict[int, int] = {}
    return np.array([seen.setdefault(int(v), len(seen)) for v in labels])


def cluster_id(prefix: str, members: Sequence[str]) -> str:
    """Id estable: mismo conjunto de miembros, mismo id entre corridas."""
    digest = hashlib.sha1("\n".join(sorted(members)).encode()).hexdigest()[:12]
    return f"{prefix}{digest}"
