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
4. si la serie no tiene NAICS (FRED casi nunca lo trae) o ningún método anterior sirve, el agregado
   más cercano por título dentro del árbol del catálogo (ADR 0020; `titulo_agregado`):
   - FRED: la misma categoría oficial (`category_path`) y, si ahí no hay, sus categorías
     ANCESTRAS (hasta `max_tree_levels` niveles). Gana el nivel más cercano con candidatas;
   - BLS, BEA y Census: la misma unidad (encuesta, tabla o dataset);
   - la candidata tiene la misma medida (fuente, frecuencia, unidades, ajuste), es al menos tan
     antigua como la joven, y su título comparte con el de la joven un PREFIJO de al menos
     `min_common_tokens` palabras (sin palabras de función). En la misma categoría el título del
     padre debe estar contenido en el de la joven (la joven lo especializa); en una categoría
     ancestra basta el prefijo común y que no sea más específico. Entre varias, el prefijo más
     largo y, a igual prefijo, el título más corto (el más general); un empate real entre títulos
     distintos es `padre_ambiguo` y no se elige.
   No se mira ningún dato: solo título, categoría, unidades, frecuencia, ajuste y fecha de inicio.
Una serie sin padre no se evalúa y el motivo queda registrado (`sin_padre`, `padre_ambiguo`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

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
    start: date | None = None  # inicio de la serie (series_meta.start_date)

    @property
    def key(self) -> Key:
        return (self.source, self.series_id)


@dataclass(frozen=True)
class Parent:
    key: Key
    how: str  # "naics_prefijo" | "total_encuesta" | "titulo_agregado"
    code: str | None = None  # NAICS del padre (None en el total)
    level: int | None = None  # niveles por encima de la categoría de la joven (titulo_agregado)


# Palabras de función de un título (no son temas): no cuentan como prefijo común.
FUNCTION_WORDS = frozenset("a an and by for from in of on or per the to with".split())


@dataclass(frozen=True)
class ParentParams:
    min_common_tokens: int = 2  # palabras mínimas del prefijo común de títulos
    max_tree_levels: int = 3  # categorías ancestras que se miran en el árbol de FRED


def title_tokens(title: str) -> tuple[str, ...]:
    """Palabras del título en minúsculas, sin puntuación ni palabras de función."""
    return tuple(
        w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if w not in FUNCTION_WORDS
    )


def common_prefix(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def category_chain(m: Meta, params: ParentParams) -> list[tuple[int, str]]:
    """(nivel, categoría) de la serie: nivel 0 = su categoría y, en FRED, sus ancestras."""
    out: dict[str, int] = {}
    for path in units_of_catalog(m):
        if m.source != "fred":
            out.setdefault(path, 0)
            continue
        parts = [p.strip() for p in path.split(">")]
        for level in range(0, min(params.max_tree_levels, len(parts) - 1) + 1):
            cat = " > ".join(parts[: len(parts) - level])
            out[cat] = min(out.get(cat, level), level)
    return sorted(((lv, c) for c, lv in out.items()), key=lambda x: (x[0], x[1]))


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
    params: ParentParams | None = None,
) -> tuple[Parent | None, str | None]:
    """(padre, motivo): `motivo` explica por qué no hay padre (None si lo hay).

    `universe`: clave -> (metadatos, código NAICS propio) de las series candidatas (las de la
    categoría de la joven y de sus ancestras). Orden de los métodos: `naics_prefijo`,
    `total_encuesta` y, si la serie no tiene NAICS o esos no sirven, `titulo_agregado`."""
    params = params or ParentParams()
    code = universe[child.key][1] if child.key in universe else None
    reason: str | None = "sin_padre"
    if code is not None:
        parent, reason = _by_naics(child, code, universe, tables)
        if parent is not None:
            return parent, None
        if reason == "padre_ambiguo":  # dos candidatas por NAICS: no se salta a otro método
            return None, reason
    by_title, why = _by_title(child, universe, params)
    if by_title is not None:
        return by_title, None
    # `padre_ambiguo` de cualquiera de los métodos se informa; si no, no había candidata.
    return None, "padre_ambiguo" if "padre_ambiguo" in (reason, why) else "sin_padre"


def _by_naics(
    child: Meta,
    code: str,
    universe: Mapping[Key, tuple[Meta, str | None]],
    tables: NaicsTables,
) -> tuple[Parent | None, str | None]:
    sig = signature(child.title, code, tables)
    cat = units_of_catalog(child)
    same: list[tuple[Meta, str | None]] = []
    for key, (m, c) in universe.items():
        if (
            key != child.key
            and same_measure(m, child)
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


def same_measure(a: Meta, b: Meta) -> bool:
    """Misma fuente, frecuencia, unidades y ajuste estacional."""
    return (
        a.source == b.source
        and a.frequency == b.frequency
        and a.units == b.units
        and a.seasonal_adjustment == b.seasonal_adjustment
    )


def _by_title(
    child: Meta,
    universe: Mapping[Key, tuple[Meta, str | None]],
    params: ParentParams,
) -> tuple[Parent | None, str | None]:
    """El agregado más cercano por título dentro del árbol de categorías (ver el docstring)."""
    child_tok = title_tokens(child.title)
    if len(child_tok) < params.min_common_tokens:
        return None, None
    by_path: dict[str, list[Key]] = defaultdict(list)
    for key, (m, _) in universe.items():
        if key != child.key:
            for path in units_of_catalog(m):
                by_path[path].append(key)
    for level, path in category_chain(child, params):
        scored: list[tuple[tuple[int, int, date, str], Meta]] = []
        for key in by_path.get(path, ()):
            m = universe[key][0]
            tok = title_tokens(m.title)
            cp = common_prefix(tok, child_tok)
            if (
                not same_measure(m, child)
                or tok == child_tok
                or cp < params.min_common_tokens
                or (m.start is not None and child.start is not None and m.start > child.start)
            ):
                continue
            if level == 0 and cp != len(tok):  # misma categoría: el padre debe estar contenido
                continue
            if level > 0 and len(tok) > len(child_tok):  # una ancestra no es más específica
                continue
            scored.append(((-cp, len(tok), m.start or date.max, m.series_id), m))
        if not scored:
            continue
        scored.sort(key=lambda x: x[0])
        top = scored[0]
        tied = [x for x in scored if x[0][:2] == top[0][:2] and x[1].title != top[1].title]
        if tied:
            return None, "padre_ambiguo"
        return Parent(top[1].key, "titulo_agregado", None, level), None
    return None, None


def index_by_family(
    universe: Mapping[Key, tuple[Meta, str | None]],
) -> dict[str, list[Key]]:
    """Índice por unidad de catálogo, para recortar el universo candidato de cada joven."""
    out: dict[str, list[Key]] = defaultdict(list)
    for key, (m, _) in universe.items():
        for u in units_of_catalog(m):
            out[u].append(key)
    return out
