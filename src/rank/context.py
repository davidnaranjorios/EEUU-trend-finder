"""Ficha del fenómeno: contexto DESCRIPTIVO de cada candidato (ADR 0009).

No es una señal: no hay p, q ni FDR, y no cambia el score, el ranking ni la regla de fuentes.

- Relacionadas: vecinas elegidas por los datos. Candidatas = series de la misma rama de
  `category_path` (misma categoría de FRED, encuesta de BLS, tabla de BEA o dataset de Census).
  Se prefiltran por coseno del título (`pool_max`) y se puntúan con
  α·coseno + (1 − α)·|ρ de crecimientos|; empate por id, así que es determinista.
  Las no descargadas se listan aparte. Nunca entran las series con copyright ni las de referencia.
- Rol (precio, cantidad, empleo, dinero, tasa) por regex genéricas sobre las UNIDADES
  (`context.roles`; la primera que casa).
- Dinámica: correlación cruzada de crecimientos entre el núcleo (mediana de sus miembros) y cada
  relacionada, con rezagos de ± `max_lag_months` (en periodos de la grilla).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.rank.cluster import GRID_ORDER, GrowthSeries, coarsen

PERIODS_PER_MONTH = {"M": 1.0, "Q": 1 / 3, "A": 1 / 12}


@dataclass
class Related:
    source: str
    series_id: str
    title: str
    units: str
    role: str
    score: float
    cos: float
    rho: float | None
    lag: int | None  # periodos de la grilla; > 0: la relacionada va detrás del núcleo
    lag_corr: float | None
    grid: str | None
    downloaded: bool


def role_of(units: str | None, roles: dict[str, str]) -> str:
    for role, pattern in roles.items():
        if units and re.search(pattern, units):
            return role
    return "otro"


def branches(category_path: str | None) -> set[str]:
    return {p for p in (category_path or "").split(";") if p}


def common_grid(a: GrowthSeries, b: GrowthSeries) -> str:
    return a.grid if GRID_ORDER[a.grid] >= GRID_ORDER[b.grid] else b.grid


def aligned(a: GrowthSeries, b: GrowthSeries) -> tuple[np.ndarray, np.ndarray, str]:
    """Crecimientos de a y b en la grilla común, sobre los periodos que comparten."""
    g = common_grid(a, b)
    ma, mb = coarsen(a, g), coarsen(b, g)
    keys = sorted(set(ma) & set(mb))
    return np.array([ma[k] for k in keys]), np.array([mb[k] for k in keys]), g


def pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    if x.size < 3 or np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def best_lag(
    core: GrowthSeries, other: GrowthSeries, max_lag_months: int, min_overlap: int
) -> tuple[int, float, str] | None:
    """Rezago (en periodos de la grilla común) con máxima |ρ|; > 0: `other` va detrás."""
    g = common_grid(core, other)
    mc, mo = coarsen(core, g), coarsen(other, g)
    max_lag = int(round(max_lag_months * PERIODS_PER_MONTH[g]))
    best: tuple[int, float, str] | None = None
    for lag in range(-max_lag, max_lag + 1):
        keys = sorted(k for k in mc if k + lag in mo)
        if len(keys) < min_overlap:
            continue
        r = pearson(np.array([mc[k] for k in keys]), np.array([mo[k + lag] for k in keys]))
        if r is None:
            continue
        if best is None or (abs(r), -abs(lag)) > (abs(best[1]), -abs(best[0])):
            best = (lag, r, g)
    return best


def median_growth(members: Sequence[GrowthSeries]) -> GrowthSeries | None:
    """Núcleo: mediana de los crecimientos de los miembros en la grilla más gruesa."""
    if not members:
        return None
    g = max((m.grid for m in members), key=lambda x: GRID_ORDER[x])
    maps = [coarsen(m, g) for m in members]
    keys = sorted(set().union(*maps))
    vals = [float(np.median([mp[k] for mp in maps if k in mp])) for k in keys]
    if not keys:
        return None
    # Índices contiguos: rellena los huecos con la mediana del periodo vecino disponible.
    full = list(range(keys[0], keys[-1] + 1))
    lookup = dict(zip(keys, vals, strict=True))
    arr = np.array([lookup.get(k, np.nan) for k in full])
    arr = np.where(np.isnan(arr), np.nanmedian(arr), arr)
    return GrowthSeries(arr, full[-1], g)


def select_related(
    core_keys: Sequence[tuple[str, str]],
    pool: Sequence[dict[str, Any]],
    core_vec: np.ndarray,
    pool_vecs: np.ndarray,
    growth: dict[tuple[str, str], GrowthSeries],
    cfg: dict[str, Any],
) -> list[Related]:
    """Relacionadas deterministas. `pool`: dicts con source, series_id, title, units, downloaded.

    `core_vec`: embedding medio (normalizado) de los títulos del núcleo; `pool_vecs`, los del pool.
    """
    c = cfg
    core = median_growth([growth[k] for k in core_keys if k in growth])
    exclude = set(core_keys)
    rows = [(i, p) for i, p in enumerate(pool) if (p["source"], p["series_id"]) not in exclude]
    if not rows:
        return []
    cos = pool_vecs @ core_vec if len(pool_vecs) else np.zeros(len(pool))
    rows.sort(key=lambda t: (-float(cos[t[0]]), t[1]["source"], t[1]["series_id"]))
    rows = rows[: c["pool_max"]]
    out: list[Related] = []
    for i, p in rows:
        key = (p["source"], p["series_id"])
        rho = lag = lag_corr = grid = None
        if p["downloaded"] and core is not None and key in growth:
            x, y, grid = aligned(core, growth[key])
            if x.size >= c["min_overlap"][grid]:
                rho = pearson(x, y)
            bl = best_lag(core, growth[key], c["max_lag_months"], c["min_overlap"][grid])
            if bl:
                lag, lag_corr, grid = bl
        score = c["alpha"] * float(cos[i]) + (1 - c["alpha"]) * abs(rho or 0.0)
        out.append(
            Related(
                p["source"],
                p["series_id"],
                p["title"] or p["series_id"],
                p.get("units") or "",
                role_of(p.get("units"), c["roles"]),
                round(score, 6),
                round(float(cos[i]), 6),
                None if rho is None else round(rho, 6),
                lag,
                None if lag_corr is None else round(lag_corr, 6),
                grid,
                bool(p["downloaded"]),
            )
        )
    downloaded = sorted(
        (r for r in out if r.downloaded), key=lambda r: (-r.score, r.source, r.series_id)
    )
    missing = sorted(
        (r for r in out if not r.downloaded), key=lambda r: (-r.cos, r.source, r.series_id)
    )
    return downloaded[: c["max_related"]] + missing[: c["max_related"]]
