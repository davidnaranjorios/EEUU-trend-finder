"""Frases en lenguaje simple generadas desde los datos (nada escrito a mano por tema).

- Cambio de pendiente: crecimiento anual en la ventana reciente frente a una ventana previa del
  mismo largo (en % si la serie es positiva; si no, en unidades por año).
- Quiebre de régimen: fecha del inicio del último régimen (PELT) y ritmo antes y después.
- Término: proporción de documentos antes de la ventana y dentro de ella.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

DAYS_PER_YEAR = 365.25


def annual_rate(
    dates: Sequence[date], values: Sequence[float], start: date, end: date
) -> tuple[float, bool] | None:
    """Pendiente anual por mínimos cuadrados entre `start` y `end`.

    Devuelve (tasa, en_porcentaje): % anual del log-nivel si todos los valores son positivos;
    si no, unidades por año. None si hay menos de 3 puntos.
    """
    pts = [(d, v) for d, v in zip(dates, values, strict=True) if start <= d <= end]
    if len(pts) < 3:
        return None
    positive = all(v > 0 for _, v in pts)
    xs = [(d - pts[0][0]).days / DAYS_PER_YEAR for d, _ in pts]
    ys = [math.log(v) if positive else v for _, v in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    return (math.expm1(slope) * 100, True) if positive else (slope, False)


def _fmt_rate(rate: tuple[float, bool], units: str) -> str:
    value, pct = rate
    if pct:
        return f"{value:+.1f} % anual"
    return f"{value:+.3g} {units or 'unidades'} por año"


def _shift(d: date, years: float) -> date:
    return date.fromordinal(d.toordinal() - round(years * DAYS_PER_YEAR))


def slope_sentence(
    dates: Sequence[date], values: Sequence[float], window: tuple[date, date], units: str
) -> str | None:
    start, end = window
    years = (end - start).days / DAYS_PER_YEAR
    recent = annual_rate(dates, values, start, end)
    before = annual_rate(dates, values, _shift(start, years), start)
    if recent is None or before is None:
        return None
    word = "creció" if recent[0] > 0 else "cayó"
    return (
        f"En los últimos {years:.0f} años {word} a {_fmt_rate(recent, units)}, frente a "
        f"{_fmt_rate(before, units)} en los {years:.0f} años previos."
    )


def regime_sentence(
    dates: Sequence[date], values: Sequence[float], change: date, units: str
) -> str | None:
    if not dates:
        return None
    before = annual_rate(dates, values, dates[0], change)
    after = annual_rate(dates, values, change, dates[-1])
    when = f"{change:%m/%Y}"
    if before is None or after is None:
        return f"Cambió de ritmo en {when}."
    return (
        f"Cambió de ritmo en {when}: pasó de {_fmt_rate(before, units)} antes a "
        f"{_fmt_rate(after, units)} después."
    )


def term_sentence(
    dates: Sequence[date], shares: Sequence[float], window_start: date, source_name: str, term: str
) -> str | None:
    before = [s for d, s in zip(dates, shares, strict=True) if d < window_start]
    inside = [s for d, s in zip(dates, shares, strict=True) if d >= window_start]
    if not before or not inside:
        return None
    b, a = sum(before) / len(before), sum(inside) / len(inside)
    return (
        f"En {source_name}, la proporción de documentos que mencionan “{term}” pasó de "
        f"{b * 100:.2f} % (promedio antes de {window_start:%Y}) a {a * 100:.2f} % desde entonces."
    )
