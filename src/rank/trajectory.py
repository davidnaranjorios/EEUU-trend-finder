"""Etiqueta de trayectoria por serie y por fenómeno (ADR 0025). Solo describe; nunca filtra.

Compara el crecimiento anual compuesto (CAGR) de la tendencia previa con el de la ventana reciente.
Todo sale de los niveles de la serie (en log) y de las recesiones
de la serie de referencia aprobada (`context.reference_series.recessions`, USREC): no hay fechas
escritas a mano.

- Ancla: el inicio de la recesión más reciente que empezó hace menos de `max_back_years` años; sin
  recesión en ese rango, `recent_years` antes del último dato.
- Tendencia previa: CAGR en los `prior_years` previos al ancla.
- Ventana reciente: CAGR del ancla al último dato.
- Pico previo: nivel máximo en los `peak_years` previos a CADA recesión del rango (ventana «antes de
  la recesión»).
- Cola: CAGR de los últimos `recent_years` años (¿se está recuperando ahora?).

Etiquetas (la serie no cae en ninguna: None):
- `rebote_bajo_pico` (tiene prioridad): el último nivel sigue bajo el pico previo, la cola (últimos
  `recent_years`) crece y la serie no venía cayendo sin parar (la tendencia previa o la ventana
  reciente es positiva): se recupera de un pozo sin haber vuelto al máximo.
- `gira_al_alza`: tendencia previa ≤ 0 y ventana reciente > 0 (ya sobre el pico previo).
- `frena_caida`: tendencia previa ≤ 0, ventana reciente ≤ 0 pero mayor: sigue cayendo, más despacio.
  NO cuenta como dirección «sube» (`phenomena.direction_of`).
- `acelera`: tendencia previa > 0, no está bajo el pico y la ventana reciente es mayor.

Etiquetas simétricas para caídas (ADR 0028). Solo se asignan si no aplica ninguna de las de arriba:
- `caida_sobre_valle`: el último nivel sigue sobre el valle previo, la cola cae y la serie no venía
  subiendo sin parar (la tendencia previa o la ventana reciente es negativa).
- `gira_a_la_baja`: tendencia previa ≥ 0 y ventana reciente < 0.
- `acelera_caida`: tendencia previa ≤ 0, la ventana reciente es menor y no está sobre el valle.
- `frena_subida`: tendencia previa > 0 y ventana reciente en [0, previa): sube más despacio.

Recuperación (ADR 0034, aprobada el 2026-11-26). Aparte de la etiqueta, cada serie lleva la marca
`recuperacion`: el último nivel está bajo su pico previo **o** bajo la extrapolación de la tendencia
de los `preshock_years` años anteriores al inicio de la última recesión (ajuste log-lineal;
tolerancia `preshock_tolerance_pct`). Sin recesión en el rango la prueba de tendencia no se evalúa.
Un fenómeno cuyas series de cantidad están todas en recuperación es de clase `recuperacion`.

`no_confirmada` es del fenómeno: ninguna de sus series cae en una etiqueta. Es una marca visible;
no filtra.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

PER_YEAR = {"M": 12, "Q": 4, "A": 1}
UP_LABELS = ("acelera", "gira_al_alza", "frena_caida", "rebote_bajo_pico")
DOWN_LABELS = ("acelera_caida", "gira_a_la_baja", "frena_subida", "caida_sobre_valle")
LABELS = UP_LABELS + DOWN_LABELS
# Marca del FENÓMENO (no de una serie): el detector ve cambio, la verificación descriptiva no.
NOT_CONFIRMED = "no_confirmada"


@dataclass(frozen=True)
class TrajectoryParams:
    prior_years: float = 5.0
    recent_years: float = 3.0
    peak_years: float = 2.0
    max_back_years: float = 10.0
    preshock_years: float = 5.0  # años previos al inicio de la última recesión (tendencia)
    preshock_tolerance_pct: float = 2.0  # «bajo la tendencia» = más de este % por debajo
    preshock_form: str = "loglinear"


def params_from(cfg: dict) -> TrajectoryParams:
    t = cfg["rank"]["trajectory"]
    return TrajectoryParams(
        float(t["prior_years"]),
        float(t["recent_years"]),
        float(t["peak_years"]),
        float(t["max_back_years"]),
        float(t["preshock_years"]),
        float(t["preshock_tolerance_pct"]),
        str(t["preshock_form"]),
    )


def recession_starts(months: Iterable[tuple[int, float]], grid: str) -> list[int]:
    """Índices de la grilla donde empieza cada recesión. `months`: (mes absoluto, indicador 0/1),
    mes absoluto = año·12 + mes − 1 (ordenado)."""
    out: list[int] = []
    prev = 0.0
    for m, v in sorted(months):
        if v >= 0.5 > prev:
            out.append(_to_grid(m, grid))
        prev = v
    return out


def _to_grid(month: int, grid: str) -> int:
    return month // (12 // PER_YEAR[grid])


def preshock_trend_gap(
    level: np.ndarray, anchor: int, p: TrajectoryParams, py: int
) -> float | None:
    """Distancia (en log) del último nivel a la extrapolación de la tendencia de los
    `preshock_years` años previos a `anchor`; None si la serie no cubre esa ventana.
    Negativa = el nivel está bajo la tendencia."""
    if p.preshock_form != "loglinear":
        raise ValueError(f"preshock_form no soportada: {p.preshock_form}")
    n = round(p.preshock_years * py)
    end = level.size - 1
    if n < 3 or anchor - n < 0 or anchor >= end:
        return None
    x = np.arange(anchor - n, anchor + 1, dtype=float)
    slope, icpt = np.polyfit(x, level[anchor - n : anchor + 1], 1)
    return float(level[end] - (icpt + slope * end))


def _cagr(level: np.ndarray, i0: int, i1: int, per_year: int) -> float | None:
    if i0 < 0 or i1 <= i0 or i1 >= level.size:
        return None
    return float(np.expm1((level[i1] - level[i0]) * per_year / (i1 - i0)))


def classify(
    levels: np.ndarray,
    first_index: int,
    grid: str,
    starts: Sequence[int],
    p: TrajectoryParams | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """(etiqueta, detalle) de una serie. `levels`: niveles reales (no el crecimiento preparado,
    que recorta los outliers y atenúa justo las caídas de las recesiones) en su grilla, el primero
    en `first_index`; `starts`: inicios de recesión en esa grilla. Con niveles ≤ 0 no se evalúa."""
    p = p or TrajectoryParams()
    py = PER_YEAR[grid]
    if levels.size < 3 or not np.isfinite(levels).all() or (levels <= 0).any():
        return None, {}
    level = np.log(levels)
    first = first_index
    end = level.size - 1
    last_index = first + end
    cands = [s for s in starts if last_index - s <= p.max_back_years * py and s < last_index]
    anchor = (max(cands) - first) if cands else end - round(p.recent_years * py)
    prior_n, peak_n, tail_n = (round(y * py) for y in (p.prior_years, p.peak_years, p.recent_years))
    g_prev = _cagr(level, anchor - prior_n, anchor, py)
    g_rec = _cagr(level, anchor, end, py)
    g_tail = _cagr(level, end - tail_n, end, py)
    # Pico previo: máximo de las ventanas de `peak_years` antes de CADA recesión del rango.
    anchors = [anchor] + [c - first for c in cands]
    peaks = [level[max(a - peak_n, 0) : a + 1].max() for a in anchors if 0 < a < end]
    below_peak = bool(peaks) and bool(level[end] < max(peaks))
    troughs = [level[max(a - peak_n, 0) : a + 1].min() for a in anchors if 0 < a < end]
    above_trough = bool(troughs) and bool(level[end] > min(troughs))
    detail: dict[str, Any] = {"cagr_previo": g_prev, "cagr_reciente": g_rec, "cagr_cola": g_tail}
    # Recuperación (ADR 0034): bajo el pico previo o bajo la tendencia pre-choque extrapolada.
    gap = preshock_trend_gap(level, anchor, p, py) if cands else None
    below_trend = None if gap is None else bool(gap < np.log1p(-p.preshock_tolerance_pct / 100))
    detail["bajo_tendencia"] = below_trend
    detail["bajo_pico"] = below_peak if peaks else None
    detail["recuperacion"] = bool(below_trend) or (below_peak if peaks else False)
    if g_prev is None or g_rec is None:
        return None, detail
    label: str | None = None
    if below_peak and g_tail is not None and g_tail > 0 and (g_prev > 0 or g_rec > 0):
        label = "rebote_bajo_pico"
    elif g_prev <= 0:
        if g_rec > 0:
            label = "gira_al_alza"
        elif g_rec > g_prev:
            label = "frena_caida"
    elif not below_peak and g_rec > g_prev:
        label = "acelera"
    if label is None:  # caídas: solo rellenan lo que las etiquetas de subida no cubren
        if above_trough and g_tail is not None and g_tail < 0 and (g_prev < 0 or g_rec < 0):
            label = "caida_sobre_valle"
        elif g_prev >= 0 and g_rec < 0:
            label = "gira_a_la_baja"
        elif g_prev <= 0 and g_rec < g_prev and not above_trough:
            label = "acelera_caida"
        elif g_prev > 0 and 0 <= g_rec < g_prev:
            label = "frena_subida"
    return label, detail


def of_phenomenon(labels: Iterable[str | None]) -> str:
    """Etiqueta más frecuente entre las series (empate: orden de `LABELS`); sin ninguna,
    `no_confirmada`."""
    c = Counter(x for x in labels if x)
    if not c:
        return NOT_CONFIRMED
    return max(c, key=lambda k: (c[k], -LABELS.index(k)))


RECOVERY_CLASS = "recuperacion"
STRUCTURAL_CLASS = "estructural"


def class_of(evidence: Iterable[tuple[str | None, str | None, bool | None]]) -> str:
    """Clase del cluster o fenómeno (ADR 0034). Cada elemento: (clase de medida, subtipo,
    marca `recuperacion` de la serie). Es `recuperacion` si TODAS sus series de cantidad (no
    poblacionales) están en recuperación; sin series de cantidad evaluables, `estructural`."""
    qty = [r for k, sub, r in evidence if k == "cantidad" and sub != "poblacion"]
    return RECOVERY_CLASS if qty and all(r is True for r in qty) else STRUCTURAL_CLASS
