"""Etiqueta "efecto de tasas" por cluster (ADR 0013; aprobada por David el 2026-11-09).

Un cluster de series de rendimientos o precios financieros puede "acelerar" solo porque subió o
bajó la tasa de corto plazo. La tasa es una serie de referencia metodológica
(`context.reference_series.short_rate`): NO entra a detectores ni al ranking, y la etiqueta nunca
filtra ni cambia el score: solo avisa.

Prueba, por cluster (sobre el crecimiento preparado de su serie representativa, el mismo de los
detectores, en su grilla; la tasa se agrega a esa grilla sumando sus cambios mensuales):

(a) Relación con la tasa. Regresión g_t = a + Σ_{k=0..L} b_k·Δr_{t−k} + e_t. El estadístico es el
    R² y su p-value sale de un stationary bootstrap por bloques del crecimiento (se conserva su
    autocorrelación y se rompe el vínculo con la tasa), con los MISMOS índices de bloques para
    todos los clusters de una misma grilla y largo. BH sobre la familia de clusters (q = fdr.q).
(b) Cambio de pendiente sin la tasa. Se repite la prueba `slope` de los detectores sobre el
    residuo (g − Σ b_k·Δr_{t−k}). Solo se marca si el cambio de pendiente original es
    significativo (p ≤ q) y el efecto cae ≥ `min_drop` o deja de ser significativo (p > q).
    Limitación: solo mira `slope`; un cluster con señal solo de `regime` no se marca.

Marca `rate_effect` si (a) y (b). `rate_share` = fracción del cambio de pendiente que explica la
tasa, 1 − efecto_residual/efecto_original recortada a [0, 1].

Sin datos de la tasa, con historia corta o en grilla anual (la línea base de `slope` no alcanza),
el cluster no se evalúa: `rate_effect` = False y `rate_share` = None (desconocido).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.detect.numeric import slope_stat, slope_test
from src.stats.bootstrap import AR_BURN_IN, family_block_length, stationary_indices
from src.stats.fdr import TailConfig, bh_adjust, bootstrap_p

PER_YEAR = {"M": 12, "Q": 4, "A": 1}


@dataclass(frozen=True)
class RateParams:
    max_lag: int  # rezagos 0..max_lag, en periodos de la grilla de la serie
    min_drop: float  # caída mínima del efecto de pendiente (0..1)
    n_resamples: int
    block_length: int | str
    seed: int
    q: float
    window: Mapping[str, int]  # slope_window por grilla
    min_baseline: int  # slope_min_baseline
    phi_margin: float
    tail: TailConfig


@dataclass
class RateResult:
    cluster_id: str
    evaluated: bool
    rate_effect: bool = False
    rate_share: float | None = None
    r2: float | None = None
    p: float | None = None
    q_bh: float | None = None
    n_tests: int | None = None
    effect_orig: float | None = None
    effect_resid: float | None = None
    p_resid: float | None = None
    p_orig: float | None = None


def rate_changes(levels: Mapping[int, float], grid: str) -> dict[int, float]:
    """Δ de la tasa por periodo completo de la grilla: suma de sus cambios mensuales."""
    per = PER_YEAR[grid]
    acc: dict[int, list[float]] = defaultdict(list)
    for m, v in levels.items():
        if m - 1 in levels:
            acc[m // (12 // per)].append(v - levels[m - 1])
    return {i: float(sum(v)) for i, v in acc.items() if len(v) == 12 // per}


def design(
    dr: Mapping[int, float], last_index: int, n: int, max_lag: int
) -> tuple[np.ndarray, np.ndarray] | None:
    """Matriz (filas útiles, 1 + rezagos) de Δr y las posiciones de `g` que la usan.

    `g` tiene `n` crecimientos que terminan en `last_index`. Se descartan los periodos sin Δr en
    algún rezago; con ellos faltantes la serie no se evalúa (None si quedan pocos)."""
    first = last_index - n + 1
    rows, pos = [], []
    for t in range(n):
        vals = [dr.get(first + t - k) for k in range(max_lag + 1)]
        if all(v is not None for v in vals):
            rows.append(vals)
            pos.append(t)
    if len(pos) < max(20, 4 * (max_lag + 2)):
        return None
    return np.asarray(rows, float), np.asarray(pos)


def _projector(x: np.ndarray) -> np.ndarray:
    """Proyector sobre las columnas centradas de x (R² sin constante aparte)."""
    xc = x - x.mean(axis=0)
    return xc @ np.linalg.pinv(xc)


def _r2(y: np.ndarray, proj: np.ndarray) -> np.ndarray:
    """R² de cada fila de y (..., m) contra el diseño cuyo proyector es `proj`."""
    yc = y - y.mean(axis=-1, keepdims=True)
    ss = (yc * yc).sum(axis=-1)
    fit = yc @ proj
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(ss > 0, (fit * yc).sum(axis=-1) / ss, 0.0)


def residual_growth(g: np.ndarray, x: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """g sin la parte lineal explicada por Δr (conserva la media); fuera de `pos`, g intacto."""
    xc = x - x.mean(axis=0)
    beta, *_ = np.linalg.lstsq(xc, g[pos] - g[pos].mean(), rcond=None)
    out = g.copy()
    out[pos] = g[pos] - xc @ beta
    return out


def evaluate(
    clusters: Mapping[str, tuple[np.ndarray, int, str, float, int]],
    dr_by_grid: Mapping[str, Mapping[int, float]],
    p: RateParams,
) -> dict[str, RateResult]:
    """Evalúa todos los clusters. `clusters`: id -> (g, last_index, grid, scale, persistence)."""
    out = {cid: RateResult(cid, False) for cid in clusters}
    usable: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for cid, (g, last, grid, _scale, _pers) in clusters.items():
        w = p.window.get(grid)
        dr = dr_by_grid.get(grid)
        # `slope` necesita ventana 2W y una línea base de al menos max(W, min_baseline).
        if not dr or w is None or g.size < 2 * w + max(w, p.min_baseline):
            continue
        d = design(dr, last, g.size, p.max_lag)
        if d is not None:
            usable[cid] = (g, *d)

    # (a) R² y su p por stationary bootstrap con índices compartidos por (grilla, largo).
    fams: dict[tuple[str, int], list[str]] = defaultdict(list)
    for cid in usable:
        fams[(clusters[cid][2], usable[cid][0].size)].append(cid)
    p_obs: dict[str, float] = {}
    for (grid, n), ids in sorted(fams.items()):
        block = family_block_length([usable[c][0] for c in ids], p.block_length)
        idx = stationary_indices(n, p.n_resamples, block, (p.seed, PER_YEAR[grid], n))
        for cid in ids:
            g, x, pos = usable[cid]
            proj = _projector(x)
            r2 = float(_r2(g[pos][None, :], proj)[0])
            null = _r2(g[idx][:, pos], proj)
            out[cid].r2 = r2
            # Misma cola GPD que los detectores: con una familia grande, el p empírico (mínimo
            # 1/(B+1)) no alcanza para que BH deje pasar ni siquiera una relación muy fuerte.
            out[cid].p = float(bootstrap_p(r2, null, p.tail)[0])
            p_obs[cid] = out[cid].p
    if not p_obs:
        return out
    order = sorted(p_obs)
    q = bh_adjust(np.array([p_obs[c] for c in order]))
    for cid, qv in zip(order, q, strict=True):
        out[cid].evaluated = True
        out[cid].q_bh, out[cid].n_tests = float(qv), len(order)

    # (b) cambio de pendiente con y sin la parte explicada por la tasa.
    for cid in order:
        r = out[cid]
        g, x, pos = usable[cid]
        _g, last, grid, scale, pers = clusters[cid]
        w = p.window[grid]
        resid = residual_growth(g, x, pos)
        r.effect_orig = float(slope_stat(g[-2 * w :], w, pers)[1])
        r.effect_resid = float(slope_stat(resid[-2 * w :], w, pers)[1])
        block = family_block_length([resid], p.block_length)
        base = g.size - w
        idx = stationary_indices(
            base, p.n_resamples, block, (p.seed, w, g.size), n_out=2 * w + AR_BURN_IN
        )
        orig = slope_test(g, scale, idx, w, pers, p.tail, p.phi_margin)
        res = slope_test(resid, scale, idx, w, pers, p.tail, p.phi_margin)
        r.p_resid = res.p_value
        if r.effect_orig != 0.0:
            r.rate_share = float(np.clip(1.0 - r.effect_resid / r.effect_orig, 0.0, 1.0))
        related = (r.q_bh or 1.0) <= p.q
        dropped = r.rate_share is not None and r.rate_share >= p.min_drop
        # Solo tiene sentido explicar un cambio de pendiente que existía: significativo antes.
        lost = orig.p_value <= p.q < res.p_value
        r.p_orig = orig.p_value
        r.rate_effect = bool(related and orig.p_value <= p.q and (dropped or lost))
    return out


def params_from(cfg: dict[str, Any]) -> RateParams:
    rc = cfg["rank"]["rate_effect"]
    d, cal = cfg["detect"], cfg["fdr"]["calibration"]
    return RateParams(
        max_lag=rc["max_lag"],
        min_drop=rc["min_drop"],
        n_resamples=cal["n_resamples"],
        block_length=cal["block_length"],
        seed=rc["seed"],
        q=float(cfg["fdr"]["q"]),
        window=d["slope_window"],
        min_baseline=d["slope_min_baseline"],
        phi_margin=d["phi_margin"],
        tail=TailConfig(**d["tail"]),
    )
