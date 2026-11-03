"""Transformaciones previas a los detectores: grilla común, log-crecimiento, ajuste estacional.

- D y W se agregan a mensual (promedio del mes): los detectores buscan tendencias de varios
  años, y así todas las series caen en una de tres grillas (M, Q, A) donde se comparten los
  índices del bootstrap.
- Se trabaja con log del nivel si la serie es positiva en toda la ventana; si no, con el nivel.
- Series sin ajuste estacional (códigos NSA*) se desestacionalizan restando al crecimiento la
  media de su periodo del año (mes o trimestre).
- Huecos internos: se rellena el nivel hacia adelante (crecimiento 0) y la serie se descarta
  si los huecos superan `max_gap_share`.
- Winsorización del crecimiento en mediana ± c·MAD: un salto aislado queda acotado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

GRID_OF = {"D": "M", "W": "M", "M": "M", "Q": "Q", "A": "A"}
PERIODS_PER_YEAR = {"M": 12, "Q": 4, "A": 1}
# Días por periodo nativo, para convertir la persistencia de D y W a meses.
NATIVE_PERIOD_DAYS = {"D": 7 / 5, "W": 7.0, "M": 30.44, "Q": 91.31, "A": 365.25}
MAD_TO_SD = 1.4826


def grid_index(d: date, grid: str) -> int:
    """Número de periodo en la grilla (meses, trimestres o años desde el año 0)."""
    if grid == "M":
        return d.year * 12 + d.month - 1
    if grid == "Q":
        return d.year * 4 + (d.month - 1) // 3
    if grid == "A":
        return d.year
    raise ValueError(f"grilla desconocida: {grid}")


def grid_date(i: int, grid: str) -> date:
    """Primer día del periodo `i` de la grilla."""
    if grid == "M":
        return date(i // 12, i % 12 + 1, 1)
    if grid == "Q":
        return date(i // 4, (i % 4) * 3 + 1, 1)
    return date(i, 1, 1)


def persistence_in_grid(native_freq: str, periods: int) -> int:
    """Periodos consecutivos exigidos, expresados en la grilla de la serie (mín. 2)."""
    grid = GRID_OF[native_freq]
    grid_days = NATIVE_PERIOD_DAYS[grid]
    return max(2, int(np.ceil(periods * NATIVE_PERIOD_DAYS[native_freq] / grid_days)))


@dataclass
class Prepared:
    """Crecimiento listo para los detectores: últimos `len(growth)` periodos de la grilla."""

    growth: np.ndarray  # winsorizado, desestacionalizado
    scale: float  # sd robusta del crecimiento (MAD)
    last_index: int  # periodo de la grilla del último nivel
    log: bool


def to_grid(indices: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, int]:
    """Nivel en una grilla densa (promedio si hay varias obs por periodo) y su primer índice."""
    ok = np.isfinite(values)
    indices, values = indices[ok], values[ok]
    if indices.size == 0:
        return np.empty(0), 0
    first = int(indices.min())
    pos = indices - first
    total = np.bincount(pos, weights=values)
    count = np.bincount(pos)
    level = np.full(total.size, np.nan)
    np.divide(total, count, out=level, where=count > 0)
    return level, first


def prepare(
    level: np.ndarray,
    first_index: int,
    n_growth: int,
    grid: str,
    *,
    seasonal_adjust: bool,
    winsor_mad: float,
    max_gap_share: float,
) -> Prepared | None:
    """Últimos `n_growth` crecimientos de una serie, o None si no alcanza o es degenerada."""
    if level.size < n_growth + 1:
        return None
    lv = level[-(n_growth + 1) :].astype(float)
    gaps = np.isnan(lv)
    if gaps[0] or gaps[-1] or gaps.mean() > max_gap_share:
        return None
    if gaps.any():  # relleno hacia adelante
        idx = np.where(~gaps, np.arange(lv.size), 0)
        lv = lv[np.maximum.accumulate(idx)]
    use_log = bool(np.all(lv > 0))
    x = np.log(lv) if use_log else lv
    g = np.diff(x)
    last_index = first_index + level.size - 1
    if seasonal_adjust and grid in ("M", "Q"):
        per = PERIODS_PER_YEAR[grid]
        # Periodo del año de cada crecimiento (el del nivel final del par).
        pos = (np.arange(last_index - n_growth + 1, last_index + 1)) % per
        means = np.array([g[pos == k].mean() if np.any(pos == k) else 0.0 for k in range(per)])
        g = g - means[pos] + g.mean()
    med = np.median(g)
    mad = np.median(np.abs(g - med)) * MAD_TO_SD
    if not np.isfinite(mad) or mad <= 0:
        return None
    g = np.clip(g, med - winsor_mad * mad, med + winsor_mad * mad)
    return Prepared(growth=g, scale=float(mad), last_index=last_index, log=use_log)
