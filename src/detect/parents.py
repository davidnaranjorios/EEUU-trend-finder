"""Agregado padre de una serie joven, elegido de metadatos y tablas oficiales (ADR 0017).

No hay mapeos escritos a mano ni listas de industrias: el padre sale de
1. la MISMA unidad de catálogo (encuesta de BLS, tabla de BEA, dataset de Census, categoría de
   FRED: `series_meta.category_path`),
2. la misma medida: misma frecuencia, unidades y ajuste estacional, y el mismo título una vez
   quitado el nombre oficial NAICS de la propia serie (queda la medida y la geografía), y
3. el anclaje NAICS de `rank/naics.py` (tablas oficiales de Census):
   - `naics_prefijo`: el padre es la serie cuyo código NAICS es un prefijo propio del código de la
     joven (menos dígitos; los rangos "31-33" cubren sus sectores); gana el prefijo más largo
     (el ancestro más cercano);
   - `total_encuesta`: si ninguna serie con NAICS sirve, el total de la unidad: la serie de la
     misma medida sin anclaje de industria. Solo si es única; con varias candidatas no se elige.
Una serie sin padre no se evalúa y el motivo queda registrado (`sin_naics`, `sin_padre`,
`padre_ambiguo`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.rank.naics import Link, NaicsTables, normalize_name

Key = tuple[str, str]  # (fuente, serie)
MIN_CODE_WEIGHT = 0.5  # un anclaje repartido entre varios sectores no define un padre


@dataclass(frozen=True)
class Meta:
    source: str
    series_id: str
    title: str
    category_path: str
    units: str
    seasonal_adjustment: str
    frequency: str

    @property
    def key(self) -> Key:
        return (self.source, self.series_id)


@dataclass(frozen=True)
class Parent:
    key: Key
    how: str  # "naics_prefijo" | "total_encuesta"
    code: str | None = None  # NAICS del padre (None en el total)


def units_of_catalog(m: Meta) -> frozenset[str]:
    """Unidades de catálogo de la serie: el primer tramo de `category_path` (la encuesta, la
    tabla o el dataset) o, en FRED, todas sus categorías."""
    parts = [p.strip() for p in (m.category_path or "").split(";") if p.strip()]
    if m.source == "fred":
        return frozenset(parts)
    return frozenset(parts[:1])


def own_code(links: Iterable[Link] | None) -> str | None:
    """Código NAICS propio de la serie: el de mayor peso si pesa ≥ MIN_CODE_WEIGHT."""
    best = max(links or [], key=lambda x: (x[1], -len(x[0])), default=None)
    return best[0] if best and best[1] >= MIN_CODE_WEIGHT else None


def signature(title: str, code: str | None, tables: NaicsTables) -> str:
    """Título sin el nombre oficial NAICS de la serie: la medida y la geografía que quedan."""
    text = normalize_name(title or "")
    name = tables.names.get(code) if code else None
    if name:
        text = text.replace(normalize_name(name), " ")
    return re.sub(r"\s+", " ", text).strip()


def _expand(code: str) -> list[str]:
    """Prefijos de un código: los rangos ("31-33") se expanden a sus sectores."""
    if "-" in code:
        lo, hi = code.split("-", 1)
        return [str(i) for i in range(int(lo), int(hi) + 1)]
    return [code]


def is_ancestor(parent: str, child: str) -> bool:
    """`parent` tiene menos dígitos que `child` y es prefijo suyo (o su sector, si es rango)."""
    return any(
        len(p) < len(child.replace("-", "")) and child.startswith(p) for p in _expand(parent)
    )


def find_parent(
    child: Meta,
    universe: Mapping[Key, tuple[Meta, str | None]],
    tables: NaicsTables,
) -> tuple[Parent | None, str | None]:
    """(padre, motivo): `motivo` explica por qué no hay padre (None si lo hay).

    `universe`: clave -> (metadatos, código NAICS propio) de todas las series candidatas."""
    code = universe[child.key][1] if child.key in universe else None
    if code is None:
        return None, "sin_naics"
    sig = signature(child.title, code, tables)
    cat = units_of_catalog(child)
    same: list[tuple[Meta, str | None]] = []
    for key, (m, c) in universe.items():
        if (
            key != child.key
            and m.source == child.source
            and m.frequency == child.frequency
            and m.units == child.units
            and m.seasonal_adjustment == child.seasonal_adjustment
            and units_of_catalog(m) & cat
            and signature(m.title, c, tables) == sig
        ):
            same.append((m, c))
    ancestors = [(m, c) for m, c in same if c is not None and is_ancestor(c, code)]
    if ancestors:
        best = max(len(c.replace("-", "")) for _, c in ancestors if c)
        top = sorted(
            (m.series_id, c) for m, c in ancestors if c and len(c.replace("-", "")) == best
        )
        if len(top) > 1 and len({c for _, c in top}) > 1:
            return None, "padre_ambiguo"
        sid, c = top[0]
        return Parent((child.source, sid), "naics_prefijo", c), None
    totals = sorted(m.series_id for m, c in same if c is None)
    if len(totals) == 1:
        return Parent((child.source, totals[0]), "total_encuesta", None), None
    return None, "padre_ambiguo" if totals else "sin_padre"


def index_by_family(
    universe: Mapping[Key, tuple[Meta, str | None]],
) -> dict[str, list[Key]]:
    """Índice por unidad de catálogo, para recortar el universo candidato de cada joven."""
    out: dict[str, list[Key]] = defaultdict(list)
    for key, (m, _) in universe.items():
        for u in units_of_catalog(m):
            out[u].append(key)
    return out
