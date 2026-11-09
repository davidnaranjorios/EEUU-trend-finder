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


def max_overlap(a: np.ndarray, b: np.ndarray, max_lag: int) -> int:
    """Mayor número de periodos comunes con dato en ambos lados entre los rezagos −L..L."""
    n = a.size
    best = 0
    for lag in range(-max_lag, max_lag + 1):
        x, y = (a[: n - lag], b[lag:]) if lag >= 0 else (a[-lag:], b[: n + lag])
        best = max(best, int((np.isfinite(x) & np.isfinite(y)).sum()))
    return best


def comove_raw(
    pairs: list[tuple[np.ndarray, np.ndarray]],
    params: ComoveParams,
    min_overlaps: list[int] | None = None,
    salt: int = 0,
) -> tuple[list[float], list[int], np.ndarray]:
    """(stat, rezago, p) de cada par sobre un eje común de largo N, sin corregir por familia.

    Los índices de bloques del stationary bootstrap son los mismos para todos los pares del eje
    (`salt` separa ejes de la misma longitud y distinta grilla)."""
    if not pairs:
        return [], [], np.zeros(0)
    mins = min_overlaps or [params.min_overlap] * len(pairs)
    n = pairs[0][0].size
    block = family_block_length(
        [np.nan_to_num(b) for _, b in pairs[: min(len(pairs), 2000)]], params.block_length
    )
    idx = stationary_indices(n, params.n_resamples, block, (params.seed, n, len(pairs), salt))
    obs, lags, p = [], [], []
    for (a, b), mo in zip(pairs, mins, strict=True):
        stat, lag = comove_stat(a, b, params.max_lag, mo)
        obs.append(stat)
        lags.append(lag)
        null = stat_matrix(a, b[idx], params.max_lag, mo)[0]
        p.append((1 + np.sum(null >= stat)) / (null.size + 1))
    return obs, lags, np.array(p)


def _rows(
    obs: list[float], lags: list[int], p_arr: np.ndarray, q_bh: np.ndarray, q_by: np.ndarray, m: int
) -> list[dict[str, float | int | str]]:
    return [
        {
            "stat": obs[i],
            "lag": lags[i],
            "p": float(p_arr[i]),
            "q_bh": float(q_bh[i]),
            "q_by": float(q_by[i]),
            "n_tests": m,
        }
        for i in range(len(obs))
    ]


def comove_family(
    pairs: list[tuple[np.ndarray, np.ndarray]],
    params: ComoveParams,
    min_overlaps: list[int] | None = None,
) -> list[dict[str, float | int | str]]:
    """Prueba toda una familia de pares (a, b) sobre un eje común de largo N.

    `min_overlaps` (opcional) fija el mínimo de periodos comunes de cada par; por defecto,
    `params.min_overlap`. Devuelve por par: stat, lag, p, q_bh, q_by, n_tests."""
    obs, lags, p_arr = comove_raw(pairs, params, min_overlaps)
    if not pairs:
        return []
    return _rows(obs, lags, p_arr, bh_adjust(p_arr), by_adjust(p_arr), len(pairs))


def comove_joint(
    groups: list[tuple[list[tuple[np.ndarray, np.ndarray]], ComoveParams, list[int]]],
) -> list[list[dict[str, float | int | str]]]:
    """Una sola familia BH sobre varios ejes (p. ej. pares trimestrales y anuales).

    Cada grupo es (pares, parámetros, mínimos de solape) sobre su propio eje, con sus propios
    índices de bloques compartidos; los p se juntan y se corrigen todos a la vez, y cada fila
    registra el tamaño de la familia completa."""
    raws = [comove_raw(pairs, prm, mins, salt=i) for i, (pairs, prm, mins) in enumerate(groups)]
    p_all = np.concatenate([r[2] for r in raws]) if raws else np.zeros(0)
    if p_all.size == 0:
        return [[] for _ in groups]
    q_bh, q_by = bh_adjust(p_all), by_adjust(p_all)
    out, lo = [], 0
    for obs, lags, p_arr in raws:
        hi = lo + len(obs)
        out.append(_rows(obs, lags, p_arr, q_bh[lo:hi], q_by[lo:hi], p_all.size))
        lo = hi
    return out


# --- Datos: ambos lados en una grilla común (el término nunca se interpola) -----------------------

PER_YEAR = {"M": 12, "Q": 4, "A": 1}
GRID_ORDER = {"M": 0, "Q": 1, "A": 2}


def coarser(a: str, b: str) -> str:
    """La grilla más gruesa de las dos: la serie se agrega a ella, el término nunca se interpola."""
    return a if GRID_ORDER[a] >= GRID_ORDER[b] else b


def series_grid_growth(
    growth: np.ndarray, last_index: int, native: str, target: str
) -> dict[int, float]:
    """Crecimiento en la grilla `target` (suma del crecimiento log de la grilla nativa).

    Solo periodos completos. Índices: años (A), año·4 + trimestre (Q) o año·12 + mes (M).
    `target` no puede ser más fina que `native` (no se interpola)."""
    if GRID_ORDER[target] < GRID_ORDER[native]:
        raise ValueError(f"no se puede llevar una serie {native} a la grilla más fina {target}")
    per = PER_YEAR[native] // PER_YEAR[target]
    first = last_index - growth.size + 1
    acc: dict[int, list[float]] = {}
    for j, g in enumerate(growth):
        if np.isfinite(g):
            acc.setdefault((first + j) // per, []).append(float(g))
    return {y: float(np.sum(v)) for y, v in acc.items() if len(v) == per}


def series_annual_growth(growth: np.ndarray, last_index: int, grid: str) -> dict[int, float]:
    """Crecimiento anual (suma del crecimiento log de la grilla nativa) de los años completos."""
    return series_grid_growth(growth, last_index, grid, "A")


def term_annual_change(counts: dict[int, tuple[float, float]]) -> dict[int, float]:
    """Δ logit (Jeffreys) de la proporción de documentos entre periodos consecutivos.

    `counts`: índice de periodo (año, o año·4 + trimestre) -> (c, n). Sirve para cualquier grilla
    de índices enteros consecutivos."""
    logit = {}
    for y, (c, n) in counts.items():
        p = (c + 0.5) / (n + 1.0)
        logit[y] = float(np.log(p / (1 - p)))
    return {y: logit[y] - logit[y - 1] for y in logit if y - 1 in logit}


term_change = term_annual_change


def on_axis(values: dict[int, float], axis: list[int]) -> np.ndarray:
    return np.array([values.get(y, np.nan) for y in axis], dtype=float)
