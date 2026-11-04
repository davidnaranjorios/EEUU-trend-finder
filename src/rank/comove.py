"""Vínculo término–serie por co-movimiento temporal (revisión 2a02c981, principio 3).

La similitud de títulos (`links.py`) solo SUGIERE pares. Un par cuenta como vínculo —y el término
como segunda fuente independiente— únicamente si pasa esta prueba:

- estadístico: máx. |ρ| entre el cambio del término (Δ logit de su proporción de documentos) y el
  crecimiento de la serie, sobre rezagos −L..L en la grilla del texto (el término puede ir antes
  o después de la serie); el máximo sobre rezagos está también en el nulo;
- nulo: stationary bootstrap del lado de la serie con **los mismos índices de bloques para todos
  los pares** de la familia (se preserva la autocorrelación de cada lado y se rompe el vínculo);
- una familia FDR propia (BH, q de `fdr.q`); BY solo se reporta como comparación.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.stats.bootstrap import family_block_length, stationary_indices
from src.stats.fdr import bh_adjust, by_adjust


@dataclass(frozen=True)
class ComoveParams:
    max_lag: int
    min_overlap: int
    n_resamples: int
    block_length: int | str
    seed: int


def stat_matrix(
    a: np.ndarray, m: np.ndarray, max_lag: int, min_overlap: int
) -> tuple[np.ndarray, np.ndarray]:
    """(máx |ρ|, rezago) de `a` (N,) contra cada fila de `m` (R, N); NaN = dato faltante.

    Rezago k > 0: la serie (fila de `m`) va k periodos después del término `a`."""
    r_rows, n = m.shape
    best = np.zeros(r_rows)
    best_lag = np.zeros(r_rows, dtype=int)
    for lag in range(-max_lag, max_lag + 1):
        x, y = (a[: n - lag], m[:, lag:]) if lag >= 0 else (a[-lag:], m[:, : n + lag])
        ok = np.isfinite(x)[None, :] & np.isfinite(y)
        cnt = ok.sum(axis=1)
        xs = np.where(ok, x[None, :], 0.0)
        ys = np.where(ok, y, 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            xm = xs.sum(axis=1, keepdims=True) / cnt[:, None]
            ym = ys.sum(axis=1, keepdims=True) / cnt[:, None]
            xc, yc = np.where(ok, xs - xm, 0.0), np.where(ok, ys - ym, 0.0)
            den = np.sqrt((xc * xc).sum(axis=1) * (yc * yc).sum(axis=1))
            rho = np.where((cnt >= min_overlap) & (den > 0), (xc * yc).sum(axis=1) / den, 0.0)
        better = np.abs(rho) > best
        best = np.where(better, np.abs(rho), best)
        best_lag = np.where(better, lag, best_lag)
    return best, best_lag


def comove_stat(a: np.ndarray, b: np.ndarray, max_lag: int, min_overlap: int) -> tuple[float, int]:
    """(máx |ρ|, rezago) entre `a` (término) y `b` (serie) en un mismo eje, con NaN si falta."""
    stat, lag = stat_matrix(a, b[None, :], max_lag, min_overlap)
    return float(stat[0]), int(lag[0])


def comove_family(
    pairs: list[tuple[np.ndarray, np.ndarray]], params: ComoveParams
) -> list[dict[str, float | int | str]]:
    """Prueba toda una familia de pares (a, b) sobre un eje común de largo N.

    Devuelve por par: stat, lag, p, q_bh, q_by, n_tests."""
    if not pairs:
        return []
    n = pairs[0][0].size
    block = family_block_length(
        [np.nan_to_num(b) for _, b in pairs[: min(len(pairs), 2000)]], params.block_length
    )
    idx = stationary_indices(n, params.n_resamples, block, (params.seed, n, len(pairs)))
    obs, lags, p = [], [], []
    for a, b in pairs:
        stat, lag = comove_stat(a, b, params.max_lag, params.min_overlap)
        obs.append(stat)
        lags.append(lag)
        null = stat_matrix(a, b[idx], params.max_lag, params.min_overlap)[0]
        p.append((1 + np.sum(null >= stat)) / (null.size + 1))
    p_arr = np.array(p)
    q_bh, q_by = bh_adjust(p_arr), by_adjust(p_arr)
    return [
        {
            "stat": obs[i],
            "lag": lags[i],
            "p": float(p_arr[i]),
            "q_bh": float(q_bh[i]),
            "q_by": float(q_by[i]),
            "n_tests": len(pairs),
        }
        for i in range(len(pairs))
    ]


# --- Datos: ambos lados en una grilla anual común ---------------------------------------------

PER_YEAR = {"M": 12, "Q": 4, "A": 1}


def series_annual_growth(growth: np.ndarray, last_index: int, grid: str) -> dict[int, float]:
    """Crecimiento anual (suma del crecimiento log de la grilla nativa) de los años completos."""
    per = PER_YEAR[grid]
    first = last_index - growth.size + 1
    acc: dict[int, list[float]] = {}
    for j, g in enumerate(growth):
        if np.isfinite(g):
            acc.setdefault((first + j) // per, []).append(float(g))
    return {y: float(np.sum(v)) for y, v in acc.items() if len(v) == per}


def term_annual_change(counts: dict[int, tuple[float, float]]) -> dict[int, float]:
    """Δ logit (Jeffreys) de la proporción anual de documentos; `counts`: año -> (c, n)."""
    logit = {}
    for y, (c, n) in counts.items():
        p = (c + 0.5) / (n + 1.0)
        logit[y] = float(np.log(p / (1 - p)))
    return {y: logit[y] - logit[y - 1] for y in logit if y - 1 in logit}


def on_axis(values: dict[int, float], axis: list[int]) -> np.ndarray:
    return np.array([values.get(y, np.nan) for y in axis], dtype=float)
