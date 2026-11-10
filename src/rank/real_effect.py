"""Efecto real de una serie en valor nominal: ¿sobrevive al deflactor? (ADR 0018).

Un monto en dólares corrientes sube con la inflación aunque la cantidad no cambie, así que el
efecto de una serie `valor` no prueba demanda por sí solo. Aquí se mide el MISMO cambio que vio el
detector pero sobre el crecimiento deflactado:

    g_real_t = g_t − π_t,   π_t = inflación del periodo (deflactor aprobado
                            `context.reference_series.deflator`, misma grilla).

`real_effect` = (media de g_real en la ventana de la señal − media de g_real en la ventana previa
de igual largo) / escala robusta del crecimiento nominal (la de los detectores). Es el mismo cambio
de medias estandarizado que `slope` (Theil-Sen del nivel acumulado ≈ crecimiento medio) y `regime`
(media después − antes) reportan como `effect_size`, así que se compara con el mismo umbral.
Sin deflactor, con una ventana previa más corta que la de la señal o sin crecimiento preparado, el
efecto queda en None (no verificable): una serie `valor` sin efecto real no cuenta como demanda.
Una serie que ya es real o encadenada se clasifica `cantidad` (ADR 0014) y no pasa por aquí.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import numpy as np

from src.rank.cluster import GrowthSeries
from src.stats.transform import grid_index


def real_effect(
    g: GrowthSeries,
    inflation: Mapping[str, Mapping[int, float]],
    window_start: date,
    window_end: date,
) -> float | None:
    """Efecto estandarizado del crecimiento deflactado en la ventana de la señal (None = no se
    pudo medir). `inflation`: grilla -> {periodo: inflación}."""
    pi = inflation.get(g.grid)
    if not pi or g.scale <= 0:
        return None
    first = g.last_index - g.growth.size + 1
    lo, hi = grid_index(window_start, g.grid), grid_index(window_end, g.grid)
    width = hi - lo + 1
    if width < 1 or hi > g.last_index or lo - width < first:
        return None  # la ventana previa de igual largo no cabe en el crecimiento preparado
    idx = np.arange(lo - width, hi + 1)
    infl = np.array([pi.get(int(i), np.nan) for i in idx])
    if np.isnan(infl).any():
        return None
    real = g.growth[idx - first] - infl
    return float((real[width:].mean() - real[:width].mean()) / g.scale)
