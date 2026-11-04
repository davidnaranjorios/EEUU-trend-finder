"""Zoom de los últimos 12 meses: contexto **descriptivo** del candidato (ADR 0009).

No es una señal: no hay p, q ni FDR, y no cambia el ranking. Sirve para leer si la tendencia de
varios años sigue en los datos más recientes (que además se revisan).

Por serie (frecuencia nativa M, W o D):
- cambio interanual: último dato frente al de 12 meses antes;
- ritmo de los últimos 12 meses (pendiente anualizada) frente a la pendiente de la ventana de
  `baseline_years` años;
- estado con una regla fija (umbrales relativos a la tendencia, `report.recent` en config.yaml):
  - `revierte`: el ritmo reciente tiene signo contrario y |reciente| ≥ reverse_frac · |tendencia|;
  - `acelera`: mismo signo y |reciente| ≥ accel_ratio · |tendencia|;
  - `se frena`: |reciente| ≤ slow_ratio · |tendencia| (o signo contrario pero pequeño);
  - `continúa`: el resto.
Q y A: `dato insuficiente` (en Q, con ≥ 4 trimestres, se muestra el interanual con aviso).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.report.narrative import annual_rate

INSUFFICIENT = "dato insuficiente"
STATES = ("acelera", "continúa", "se frena", "revierte")
HIGH_FREQ = ("M", "W", "D")


@dataclass
class Recent:
    state: str
    yoy: tuple[float, bool] | None  # (cambio, en %) último vs 12 meses antes
    recent_rate: tuple[float, bool] | None  # pendiente anualizada de los últimos 12 meses
    trend_rate: tuple[float, bool] | None  # pendiente anualizada de la ventana larga
    note: str | None = None
    zoom: tuple[list[date], list[float]] | None = None  # últimos `chart_months`
    highlight: tuple[date, date] | None = None


def _months_before(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, 28))


def _value_at_or_before(
    dates: Sequence[date], values: Sequence[float], target: date
) -> float | None:
    best = None
    for d, v in zip(dates, values, strict=True):
        if d <= target:
            best = v
        else:
            break
    return best


def yoy_change(dates: Sequence[date], values: Sequence[float]) -> tuple[float, bool] | None:
    if len(dates) < 2:
        return None
    last, prev = values[-1], _value_at_or_before(dates, values, _months_before(dates[-1], 12))
    if prev is None or dates[0] > _months_before(dates[-1], 12):
        return None
    if prev > 0 and last > 0:
        return (last / prev - 1) * 100, True
    return last - prev, False


def classify(recent: float, trend: float, cfg: dict[str, Any]) -> str:
    """Regla fija del estado (umbrales relativos a la tendencia, ver módulo)."""
    if trend == 0:
        return "continúa"
    ratio = recent / trend  # > 0: mismo signo
    if ratio < 0 and abs(ratio) >= cfg["reverse_frac"]:
        return "revierte"
    if ratio >= cfg["accel_ratio"]:
        return "acelera"
    if ratio <= cfg["slow_ratio"]:
        return "se frena"
    return "continúa"


def series_recent(
    dates: Sequence[date], values: Sequence[float], frequency: str, cfg: dict[str, Any]
) -> Recent:
    if not dates:
        return Recent(INSUFFICIENT, None, None, None, "sin datos")
    last = dates[-1]
    if frequency not in HIGH_FREQ:
        yoy = None
        note = "frecuencia trimestral o anual: el zoom de 12 meses no aplica"
        if frequency == "Q" and sum(d > _months_before(last, 12) for d in dates) >= 4:
            yoy = yoy_change(dates, values)
            note = "trimestral: solo el cambio interanual (4 trimestres), con cautela"
        return Recent(INSUFFICIENT, yoy, None, None, note)
    start12 = _months_before(last, cfg["months"])
    recent = annual_rate(dates, values, start12, last)
    trend = annual_rate(dates, values, _months_before(last, 12 * cfg["baseline_years"]), last)
    zoom_start = _months_before(last, cfg["chart_months"])
    zoom = (
        [d for d in dates if d >= zoom_start],
        [v for d, v in zip(dates, values, strict=True) if d >= zoom_start],
    )
    if recent is None or trend is None or recent[1] != trend[1]:
        return Recent(INSUFFICIENT, yoy_change(dates, values), recent, trend, None, zoom)
    return Recent(
        classify(recent[0], trend[0], cfg),
        yoy_change(dates, values),
        recent,
        trend,
        None,
        zoom,
        (start12, last),
    )


def term_recent(
    dates: Sequence[date], shares: Sequence[float], grid: str
) -> tuple[float, float] | None:
    """Proporción media del último año de la grilla frente al año anterior (A: 1, Q: 4, M: 12)."""
    per_year = {"A": 1, "Q": 4, "M": 12}.get(grid, 1)
    if len(shares) < 2 * per_year:
        return None
    last = shares[-per_year:]
    prev = shares[-2 * per_year : -per_year]
    return sum(last) / len(last), sum(prev) / len(prev)


def summary_state(states: Sequence[str]) -> str:
    """Estado del candidato: el más frecuente entre sus series con dato (empate: mixto)."""
    counts = Counter(s for s in states if s in STATES)
    if not counts:
        return INSUFFICIENT
    top = counts.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return "mixto"
    return top[0][0]
