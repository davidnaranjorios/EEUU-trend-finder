"""Etiqueta "efecto de tasas" por cluster (ADR 0013; aprobada por David el 2026-11-09).

Un cluster de series de rendimientos o precios financieros puede "acelerar" solo porque subió o
bajó la tasa de corto plazo. La tasa es una serie de referencia metodológica
(`context.reference_series.short_rate`): NO entra a detectores ni al ranking, y la etiqueta nunca
filtra ni cambia el score: solo avisa.

Prueba, por cluster (sobre el crecimiento preparado de su serie representativa, el mismo de los
detectores, en su grilla; la tasa se agrega a esa grilla sumando sus cambios mensuales):

(a) Relación con la tasa. Regresión g_t = a + c·r_t + Σ_{k=0..L} b_k·Δr_{t−k} + e_t, con r_t el
    NIVEL de la tasa (promedio del periodo) y Δr su cambio (suma de los cambios mensuales). El
    nivel hace falta porque el crecimiento de un índice de acumulación (T-bills, money market,
    intereses devengados) es ≈ r_t / periodos por año: depende del nivel, y su cambio de
    pendiente es justo un cambio de nivel de la tasa. Con solo Δr el R² es bajo y el residuo
    conserva la aceleración. Se eligió el nivel contemporáneo (lag 0) más Δr con rezagos 0..L:
    cubre el devengo del periodo y la respuesta rezagada a los cambios, con 1 + (L+1) columnas en
    vez de 2·(L+1) niveles y cambios casi colineales. `include_level: false` reproduce el diseño
    anterior (solo Δr). El estadístico es el R² y su p-value sale de un stationary bootstrap por
    bloques del crecimiento (se conserva su autocorrelación y se rompe el vínculo con la tasa),
    con los MISMOS índices de bloques para todos los clusters de una misma grilla y largo.
    BH sobre la familia de clusters (q = fdr.q). Como el nivel de la tasa es persistente, un
    ajuste en niveles puede ser espurio (una aceleración propia coincide por azar con su deriva:
    medido, 6 de 40 marcas falsas sin la condición siguiente). Por eso también se exige que el
    MISMO modelo en primeras diferencias (Δg sobre Δr y Δ²r, estacionarias) sea significativo,
    con su propio bootstrap de Δg y su propia familia BH: si g = c·r entonces Δg = c·Δr.
(b) Cambio de pendiente sin la tasa. Se repite la prueba `slope` de los detectores sobre el
    residuo (g − c·r_t − Σ b_k·Δr_{t−k}). Solo se marca si el cambio de pendiente original es
    significativo (p ≤ q) y el efecto cae ≥ `min_drop` o deja de ser significativo (p > q).
    Limitación: solo mira `slope`; un cluster con señal solo de `regime` no se marca.

Marca `rate_effect` si (a) y (b). `rate_share` = fracción del cambio de pendiente que explica la
tasa, 1 − efecto_residual/efecto_original recortada a [0, 1].

Sin datos de la tasa, con historia corta o en grilla anual (la línea base de `slope` no alcanza),
el cluster no se evalúa: `rate_effect` = False y `rate_share` = None (desconocido) y `reason`
dice por qué (`sin_tasa`, `sin_ventana`, `serie_corta`, `pocos_periodos_con_tasa`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    include_level: bool = True  # nivel contemporáneo de la tasa además de Δr (False: solo Δr)
    min_rows: Mapping[str, int] = field(default_factory=lambda: {"M": 36, "Q": 20, "A": 12})


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
    r2_diff: float | None = None  # R² del mismo modelo en primeras diferencias
    p_diff: float | None = None
    q_diff: float | None = None
    reason: str | None = None  # por qué no se evaluó (None si se evaluó)


def rate_changes(levels: Mapping[int, float], grid: str) -> dict[int, float]:
    """Δ de la tasa por periodo completo de la grilla: suma de sus cambios mensuales."""
    per = PER_YEAR[grid]
    acc: dict[int, list[float]] = defaultdict(list)
    for m, v in levels.items():
        if m - 1 in levels:
            acc[m // (12 // per)].append(v - levels[m - 1])
    return {i: float(sum(v)) for i, v in acc.items() if len(v) == 12 // per}


def rate_levels(levels: Mapping[int, float], grid: str) -> dict[int, float]:
    """Nivel de la tasa por periodo completo de la grilla: promedio de sus meses."""
    step = 12 // PER_YEAR[grid]
    acc: dict[int, list[float]] = defaultdict(list)
    for m, v in levels.items():
        acc[m // step].append(v)
    return {i: float(np.mean(v)) for i, v in acc.items() if len(v) == step}


def design(
    dr: Mapping[int, float],
    last_index: int,
    n: int,
    max_lag: int,
    lv: Mapping[int, float] | None = None,
    min_rows: int = 20,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Matriz (filas útiles, [nivel] + rezagos de Δr) y las posiciones de `g` que la usan.

    `g` tiene `n` crecimientos que terminan en `last_index`. Con `lv` (niveles de la tasa) la
    primera columna es el nivel contemporáneo; sin él, el diseño es solo Δr. Se descartan los
    periodos sin dato en alguna columna; si quedan menos de `min_rows` (o de 4 filas por
    parámetro) la serie no se evalúa (None)."""
    first = last_index - n + 1
    rows, pos = [], []
    for t in range(n):
        vals = [dr.get(first + t - k) for k in range(max_lag + 1)]
        if lv is not None:
            vals = [lv.get(first + t), *vals]
        if all(v is not None for v in vals):
            rows.append(vals)
            pos.append(t)
    ncols = max_lag + 1 + (lv is not None)
    if len(pos) < max(min_rows, 4 * (ncols + 1)):
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


def diff_design(
    g: np.ndarray, x: np.ndarray, pos: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """El mismo modelo en primeras diferencias: (Δg completo con NaN, ΔX, posiciones de Δg).

    Solo se usan pares de filas consecutivas. Dos series persistentes (el nivel de la tasa y una
    aceleración propia) se ajustan en niveles por azar (regresión espuria); en diferencias, que
    son estacionarias, solo queda la relación real: si g = c·r entonces Δg = c·Δr."""
    dg = np.full(g.size, np.nan)
    dg[1:] = np.diff(g)
    keep = np.flatnonzero(np.diff(pos) == 1) + 1  # filas cuya anterior también está en el diseño
    return dg, np.diff(x, axis=0)[keep - 1], pos[keep]


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
    lv_by_grid: Mapping[str, Mapping[int, float]] | None = None,
) -> dict[str, RateResult]:
    """Evalúa todos los clusters. `clusters`: id -> (g, last_index, grid, scale, persistence).

    `lv_by_grid` (niveles de la tasa por grilla) es obligatorio con `include_level`; los clusters
    que no se pueden evaluar quedan con `reason`."""
    out = {cid: RateResult(cid, False) for cid in clusters}
    usable: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for cid, (g, last, grid, _scale, _pers) in clusters.items():
        w = p.window.get(grid)
        dr = dr_by_grid.get(grid)
        lv = (lv_by_grid or {}).get(grid) if p.include_level else None
        if w is None:
            out[cid].reason = "sin_ventana"
            continue
        if not dr or (p.include_level and not lv):
            out[cid].reason = "sin_tasa"
            continue
        # `slope` necesita ventana 2W y una línea base de al menos max(W, min_baseline).
        if g.size < 2 * w + max(w, p.min_baseline):
            out[cid].reason = "serie_corta"
            continue
        d = design(dr, last, g.size, p.max_lag, lv, int(p.min_rows.get(grid, 20)))
        if d is None:
            out[cid].reason = "pocos_periodos_con_tasa"
            continue
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
            # Mismo modelo en primeras diferencias, con su propio bootstrap de Δg (estacionario).
            dg, xd, dpos = diff_design(g, x, pos)
            didx = stationary_indices(n - 1, p.n_resamples, block, (p.seed, PER_YEAR[grid], n, 1))
            proj_d = _projector(xd)
            r2d = float(_r2(dg[dpos][None, :], proj_d)[0])
            dnull = _r2(np.where(np.isfinite(dg[1:]), dg[1:], 0.0)[didx][:, dpos - 1], proj_d)
            out[cid].r2_diff = r2d
            out[cid].p_diff = float(bootstrap_p(r2d, dnull, p.tail)[0])
    if not p_obs:
        return out
    order = sorted(p_obs)
    q = bh_adjust(np.array([p_obs[c] for c in order]))
    qd = bh_adjust(np.array([out[c].p_diff for c in order]))
    for cid, qv, qdv in zip(order, q, qd, strict=True):
        out[cid].evaluated = True
        out[cid].q_bh, out[cid].n_tests = float(qv), len(order)
        out[cid].q_diff = float(qdv)

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
        # Relación en niveles Y en diferencias (la segunda descarta el ajuste espurio de niveles).
        related = (r.q_bh or 1.0) <= p.q and (r.q_diff or 1.0) <= p.q
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
        include_level=bool(rc.get("include_level", True)),
        min_rows=dict(rc.get("min_rows_by_grid") or {}),
    )
