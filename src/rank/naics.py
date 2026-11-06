"""Anclaje a industria NAICS (ADR 0012). Todo sale de tablas oficiales de Census o de los datos.

Métodos (columna `metodo` de `naics_links`):
- `codigo_en_metadatos`: código NAICS explícito ("NAICS 3361") en título o ruta de categoría de la
  serie; solo vale si el código existe en la tabla oficial NAICS 2022.
- `heredado_cluster`: una serie sin código hereda los NAICS de los miembros de su cluster que sí
  lo tienen (peso = promedio de sus pesos).
- `sic_concordancia`: un término de texto recibe los NAICS de las empresas que lo mencionan: SIC de
  4 dígitos (EDGAR, `term_freq.sic_top_json`) → NAICS con la concordancia oficial de Census,
  ponderado por documentos. Sin SIC medido (esquema < 5) no se inventa nada: queda pendiente.

No hay mapeos escritos a mano: `config/naics/*.csv` son las tablas oficiales (ver SOURCES.md).
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# "NAICS 3361", "NAICS code: 3361", "NAICS2017: 31-33", "2017 NAICS code 3361".
CODE_RE = re.compile(
    r"NAICS(?:\s*(?:19|20)\d\d)?(?:\s+(?:industry\s+)?codes?)?\s*[:#=]?\s*"
    r"(\d{2}-\d{2}|\d{2,6})\b",
    re.IGNORECASE,
)
NAME_SPLIT_RE = re.compile(r"\s*(?:\||;|:|,|\s-\s|\s–\s)\s*")

Link = tuple[str, float, str]  # (naics, peso, metodo)


@dataclass(frozen=True)
class NaicsTables:
    names: dict[str, str]  # código NAICS 2022 -> nombre oficial
    by_name: dict[str, str]  # nombre oficial normalizado -> código (el más amplio si se repite)
    concordance: dict[str, list[tuple[str, float]]]  # SIC (4 dígitos) -> [(NAICS 2022, peso)]


def load_tables(cfg: Mapping[str, Any], root: Path) -> NaicsTables:
    paths = cfg["rank"]["naics"]
    with (root / paths["names_table"]).open(encoding="utf-8", newline="") as fh:
        names = {r["naics"]: r["name"] for r in csv.DictReader(fh)}
    conc: dict[str, list[tuple[str, float]]] = defaultdict(list)
    with (root / paths["concordance_table"]).open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            conc[r["sic"]].append((r["naics"], float(r["weight"])))
    by_name: dict[str, str] = {}
    for code, name in sorted(names.items(), key=lambda kv: (len(kv[0]), kv[0])):
        by_name.setdefault(normalize_name(name), code)
    return NaicsTables(names, by_name, dict(conc))


def normalize_name(text: str) -> str:
    """Minúsculas, sin paréntesis ni puntuación: para comparar contra nombres oficiales."""
    text = re.sub(r"\([^)]*\)", " ", text.lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def names_in_text(text: str, tables: NaicsTables) -> list[str]:
    """Códigos cuyo nombre oficial NAICS es exactamente un segmento del texto (título/ruta)."""
    out: list[str] = []
    for seg in NAME_SPLIT_RE.split(text or ""):
        key = normalize_name(seg)
        code = tables.by_name.get(key) if key else None
        if code and code not in out:
            out.append(code)
    return out


def codes_in_text(text: str, tables: NaicsTables) -> list[str]:
    """Códigos NAICS explícitos en `text` que existen en la tabla oficial (sin duplicados)."""
    out: list[str] = []
    for m in CODE_RE.finditer(text or ""):
        code = m.group(1)
        if code in tables.names and code not in out:
            out.append(code)
    return out


def series_links(
    meta_text: Mapping[tuple[str, str], str], tables: NaicsTables
) -> dict[tuple[str, str], list[Link]]:
    """`codigo_en_metadatos`: peso uniforme entre los códigos explícitos de cada serie."""
    out = {}
    for key, text in meta_text.items():
        codes = codes_in_text(text, tables)
        method = "codigo_en_metadatos"
        if not codes:
            codes, method = names_in_text(text, tables), "nombre_oficial"
        if codes:
            out[key] = [(c, 1 / len(codes), method) for c in codes]
    return out


def inherit_by_origin(
    duplicates: Mapping[tuple[str, str], Iterable[tuple[str, str]]],
    own: Mapping[tuple[str, str], list[Link]],
) -> dict[tuple[str, str], list[Link]]:
    """`heredado_origen`: una serie sin NAICS que es copia de otra agencia (ADR 0010) hereda."""
    out: dict[tuple[str, str], list[Link]] = {}
    for key, copies in duplicates.items():
        donors = [c for c in copies if c in own]
        if key in own or not donors:
            continue
        acc: dict[str, float] = defaultdict(float)
        for d in donors:
            for naics, w, _ in own[d]:
                acc[naics] += w / len(donors)
        out[key] = [(n, w, "heredado_origen") for n, w in sorted(acc.items())]
    return out


def inherit_in_clusters(
    clusters: Iterable[list[tuple[str, str]]],
    own: Mapping[tuple[str, str], list[Link]],
) -> dict[tuple[str, str], list[Link]]:
    """`heredado_cluster`: miembros sin NAICS reciben el promedio de los de su cluster."""
    out: dict[tuple[str, str], list[Link]] = {}
    for members in clusters:
        donors = [k for k in members if k in own]
        if not donors:
            continue
        acc: dict[str, float] = defaultdict(float)
        for k in donors:
            for naics, w, _ in own[k]:
                acc[naics] += w / len(donors)
        links = [(n, w, "heredado_cluster") for n, w in sorted(acc.items())]
        for k in members:
            if k not in own:
                out[k] = links
    return out


def term_links(
    sic_counts: Mapping[tuple[str, str], Mapping[str, int]], tables: NaicsTables
) -> dict[tuple[str, str], list[Link]]:
    """`sic_concordancia`: documentos por SIC → NAICS oficiales, normalizado a suma 1."""
    out = {}
    for key, counts in sic_counts.items():
        acc: dict[str, float] = defaultdict(float)
        for sic, n in counts.items():
            for naics, w in tables.concordance.get(sic, ()):
                acc[naics] += n * w
        total = sum(acc.values())
        if total > 0:
            out[key] = [(c, v / total, "sic_concordancia") for c, v in sorted(acc.items())]
    return out


def parse_sic_top(payloads: Iterable[str | None]) -> dict[str, int]:
    """Suma los JSON {sic: documentos} de varios periodos."""
    total: dict[str, int] = defaultdict(int)
    for p in payloads:
        if p:
            for sic, n in json.loads(p).items():
                total[sic] += int(n)
    return dict(total)


def coverage(
    objects: Iterable[tuple[str, str]],
    links: Mapping[tuple[str, str], list[Link]],
    not_applicable: Iterable[str] = (),
) -> dict[str, dict[str, float | int]]:
    """% de objetos con señal que tienen NAICS, por fuente (las no aplicables quedan fuera)."""
    skip = set(not_applicable)
    tot: dict[str, int] = defaultdict(int)
    hit: dict[str, int] = defaultdict(int)
    for src, oid in objects:
        if src in skip:
            continue
        tot[src] += 1
        hit[src] += (src, oid) in links
    return {
        s: {"n": tot[s], "with_naics": hit[s], "pct": round(100 * hit[s] / tot[s], 1)}
        for s in sorted(tot)
    }


def rows(
    run_id: str, links: Mapping[tuple[str, str], list[Link]], tables: NaicsTables
) -> list[dict[str, Any]]:
    return [
        {
            "run_id": run_id,
            "source": src,
            "object_id": oid,
            "naics": naics,
            "naics_name": tables.names.get(naics),
            "peso": float(w),
            "metodo": method,
        }
        for (src, oid), ls in sorted(links.items())
        for naics, w, method in ls
    ]
