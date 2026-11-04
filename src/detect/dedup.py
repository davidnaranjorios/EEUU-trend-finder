"""Deduplicación ANTES de probar (revisión 702b8ff3).

Una misma variable puede llegar varias veces: el mismo código base de BEA en varias tablas NIPA
(p. ej. `Y033RG` en T50306 y T50305), o la misma serie en FRED y en la API original. Contarlas
por separado infla la familia FDR y hace aparecer un fenómeno varias veces.

Dos series son la misma variable si comparten clave base (código de serie, sin tabla ni fuente),
grilla y geografía **y** sus valores coinciden donde se solapan. La clave sola no basta: una
coincidencia de nombre con datos distintos no se une. Se prueba una representante por grupo (la
de más historia; a igualdad, la fuente original antes que FRED); las demás se listan en la ficha
pero no cuentan como pruebas ni como miembros del cluster.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import numpy as np

# Código de serie de BEA: mezcla de letras y dígitos (no un índice de línea).
_CODE = re.compile(r"^(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{4,}$")
SOURCE_RANK = {"bls": 0, "bea": 0, "census": 0, "fred": 1}


def base_key(source: str, series_id: str, grid: str) -> tuple[str, ...] | None:
    """Clave de variable sin tabla ni fuente; None si la serie no se puede casar."""
    if source == "bea":
        parts = series_id.split(":")
        if len(parts) < 4 or not _CODE.match(parts[2]):
            return None
        return ("code", parts[2].upper(), ":".join(parts[3:-1]), parts[-1], grid)
    return ("code", series_id.upper(), "", "", grid)


def _same_values(a: Any, b: Any) -> bool:
    lo = max(a.first_index, b.first_index)
    hi = min(a.first_index + a.level.size, b.first_index + b.level.size)
    if hi - lo < 4:
        return False
    x = a.level[lo - a.first_index : hi - a.first_index]
    y = b.level[lo - b.first_index : hi - b.first_index]
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 4:
        return False
    return bool(np.allclose(x[ok], y[ok], rtol=1e-6, atol=1e-9))


def dedup_series(
    series: Sequence[Any],
) -> tuple[list[Any], dict[tuple[str, str], list[tuple[str, str]]]]:
    """(representantes, {(fuente, id) de la representante: miembros omitidos})."""
    buckets: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    reps: list[Any] = []
    for s in series:
        key = base_key(s.source, s.series_id, s.grid)
        if key is None:
            reps.append(s)
        else:
            buckets[key].append(s)
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for members in buckets.values():
        order = sorted(
            members,
            key=lambda s: (-s.level.size, SOURCE_RANK.get(s.source, 1), s.source, s.series_id),
        )
        clusters: list[list[Any]] = []
        for s in order:
            for c in clusters:
                if _same_values(c[0], s):
                    c.append(s)
                    break
            else:
                clusters.append([s])
        for c in clusters:
            reps.append(c[0])
            if len(c) > 1:
                groups[(c[0].source, c[0].series_id)] = [(m.source, m.series_id) for m in c[1:]]
    reps.sort(key=lambda s: (s.source, s.series_id))
    return reps, groups
