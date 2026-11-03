"""Identidad de fuente para la triangulación (principio 4, ADR 0009).

Una evidencia (serie con señal o término emergente) lleva un conjunto de agencias:
- serie de FRED: las agencias de sus releases (`fred_series_attrs.agencies`);
- serie de BLS, BEA o Census: la agencia que declara su adaptador (`AGENCY`, con el nombre de FRED);
- término de texto: la fuente de texto (`texto:edgar`, `texto:federal_register`).

Dos evidencias son independientes solo si no comparten agencia, ni directamente ni a través de
otras evidencias. Por eso el número de fuentes independientes de un conjunto de evidencias es el
número de componentes conexas del grafo que une las agencias que aparecen en una misma evidencia
(conservador: el mismo dato de BLS bajado de FRED y de BLS es una sola fuente). Una evidencia sin
agencia conocida no cuenta.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from src.ingest import bea, bls, census

# Fuente de ingesta -> agencia declarada por su adaptador (FRED se resuelve serie por serie).
ADAPTER_AGENCIES = {m.SOURCE: m.AGENCY for m in (bls, bea, census)}
TEXT_PREFIX = "texto:"
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def agency_key(name: str) -> str:
    """Nombre de agencia normalizado: minúsculas, sin puntuación ni el prefijo "U.S."."""
    tokens = _NON_ALNUM.sub(" ", name.casefold()).split()
    if tokens[:2] == ["u", "s"]:
        tokens = tokens[2:]
    elif tokens[:1] == ["us"]:
        tokens = tokens[1:]
    return " ".join(tokens)


def series_agencies(source: str, fred_agencies: Iterable[str] | None = None) -> frozenset[str]:
    """Agencias de una serie numérica (vacío si se desconocen)."""
    if source in ADAPTER_AGENCIES:
        return frozenset({agency_key(ADAPTER_AGENCIES[source])})
    return frozenset(agency_key(a) for a in (fred_agencies or []) if a and agency_key(a))


def text_agencies(text_source: str) -> frozenset[str]:
    return frozenset({TEXT_PREFIX + text_source})


def independent_groups(agency_sets: Iterable[frozenset[str]]) -> list[frozenset[str]]:
    """Componentes conexas de agencias: cada una es una fuente independiente."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for agencies in agency_sets:
        items = sorted(agencies)
        for a in items:
            parent.setdefault(a, a)
        for a in items[1:]:
            ra, rb = find(items[0]), find(a)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, set[str]] = {}
    for a in parent:
        groups.setdefault(find(a), set()).add(a)
    return sorted((frozenset(g) for g in groups.values()), key=lambda g: sorted(g))


def n_independent(agency_sets: Iterable[frozenset[str]]) -> int:
    return len(independent_groups(agency_sets))


def unmatched_adapter_agencies(fred_agency_names: Iterable[str]) -> list[str]:
    """Agencias de adaptadores que no aparecen entre las de FRED (el solape no se detectaría)."""
    known = {agency_key(a) for a in fred_agency_names if a}
    if not known:
        return []
    return sorted(s for s, a in ADAPTER_AGENCIES.items() if agency_key(a) not in known)
