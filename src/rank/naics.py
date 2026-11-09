"""Anclaje a industria NAICS (ADR 0012). Todo sale de tablas oficiales de Census o de los datos.

Métodos (columna `metodo` de `naics_links`):
- `codigo_en_metadatos`: código NAICS explícito ("NAICS 3361") en título o ruta de categoría de la
  serie; solo vale si el código existe en la tabla oficial NAICS 2022.
- `codigo_en_id`: el código está en el id propio de la serie (PPI de BLS `PCU<naics><producto>`,
  JOLTS `JT<S|U><industria>...`); solo vale si existe en la tabla oficial (los ceros de relleno a
  la derecha se recortan: 524100 → 5241).
- `codigo_en_id` también lee Census: la dimensión de industria de una serie (`NAICS`, `NAICS2017`,
  `NAICS2022`, `category_code` de EITS, `industry` de QWI) aparece en el id como `col=valor`.
  Un código de NAICS 2017 que cambió en 2022 se pasa con la concordancia oficial de Census.
- `concordancia_bea`: el id de BEA lleva su código de industria (`GDPbyIndustry:1:111CA:A`,
  `InputOutput:57:111CA.Industry.487OS.Commodity:A`); la tabla oficial de BEA lo lleva a NAICS 2022
  (`config/naics/bea_industry_naics.csv`, peso repartido). Los agregados sin NAICS quedan sin fila.
- `nombre_oficial`: un tramo del título (limitado por comas, "and" o separadores) es el nombre
  oficial NAICS 2022 COMPLETO. Un título que lista varios sectores reparte el peso entre todos los
  que casan; los fragmentos de un nombre más largo no cuentan.
- `heredado_cluster`: una serie sin código hereda del cluster solo los NAICS que reúnen más de
  `rank.naics.cluster_majority` del peso de los miembros con código propio. Es una estimación.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# "NAICS 3361", "NAICS code: 3361", "NAICS2017: 31-33", "2017 NAICS code 3361".
CODE_RE = re.compile(
    r"NAICS(?:\s*(?:19|20)\d\d)?(?:\s+(?:industry\s+)?codes?)?\s*[:#=]?\s*"
    r"(\d{2}-\d{2}|\d{2,6})\b",
    re.IGNORECASE,
)
# Separadores fuertes: parten el título en tramos independientes (la coma NO, porque los nombres
# oficiales la llevan: "Arts, Entertainment, and Recreation").
NAME_SPLIT_RE = re.compile(r"\s*(?:\||;|:|\s-\s|\s–\s)\s*")
TOKEN_RE = re.compile(r"[a-z0-9]+|,")
# Formatos de id de BLS con el código de industria dentro: (fuentes donde aplica, regex).
# PPI industria: PCU + industria (2–6 dígitos, o "31-33" con guiones de relleno) + producto.
# JOLTS: JT + S|U + industria (6 dígitos) + estado, área, tamaño, elemento y tasa.
ID_FORMATS: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = (
    (frozenset({"bls", "fred"}), re.compile(r"^PCU(\d{2}-\d{2}(?=-)|\d{2,6})")),
    (frozenset({"bls"}), re.compile(r"^JT[SU](\d{6})\d{9}[A-Z]{2}[A-Z]$")),
)

Link = tuple[str, float, str]  # (naics, peso, metodo)


# Dimensiones de industria de Census en el id (`col=valor`): nombre de la columna, no un tema.
CENSUS_COL_RE = re.compile(r"(?i)^(?:naics(?:\d{4})?|category_code|industry)$")
# Ids de BEA con el código de industria dentro (ver `bea_codes`).
BEA_IO_RE = re.compile(r"^InputOutput:[^:]+:([^:]+):[A-Za-z]$")
BEA_INDUSTRY_RE = re.compile(r"^(?:Underlying)?GDPbyIndustry:[^:]+:([A-Za-z0-9]+):[A-Za-z]$")
BEA_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,6}$")


@dataclass(frozen=True)
class NaicsTables:
    names: dict[str, str]  # código NAICS 2022 -> nombre oficial
    by_name: dict[str, str]  # nombre oficial normalizado -> código (el más amplio si se repite)
    concordance: dict[str, list[tuple[str, float]]]  # SIC (4 dígitos) -> [(NAICS 2022, peso)]
    bea: dict[str, list[tuple[str, float]]] = field(default_factory=dict)  # código BEA -> NAICS
    to2022: dict[str, list[tuple[str, float]]] = field(default_factory=dict)  # NAICS 2017 -> 2022


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
    return NaicsTables(
        names,
        by_name,
        dict(conc),
        _pairs(root / paths["bea_table"], "bea_code", "naics", "weight"),
        _pairs(root / paths["concordance_2017_table"], "naics2017", "naics2022", "weight"),
    )


def _pairs(path: Path, key: str, value: str, weight: str) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            out[r[key]].append((r[value], float(r[weight])))
    return dict(out)


def normalize_name(text: str) -> str:
    """Minúsculas, sin paréntesis ni puntuación: para comparar contra nombres oficiales."""
    text = re.sub(r"\([^)]*\)", " ", text.lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _tokens(segment: str) -> tuple[list[str], list[bool]]:
    """Palabras normalizadas del tramo y, por palabra, si una coma la sigue (sin paréntesis)."""
    raw = TOKEN_RE.findall(re.sub(r"\([^)]*\)", " ", (segment or "").lower()))
    words: list[str] = []
    comma_after: list[bool] = []
    for tok in raw:
        if tok == ",":
            if comma_after:
                comma_after[-1] = True
        else:
            words.append(tok)
            comma_after.append(False)
    return words, comma_after


def names_in_text(text: str, tables: NaicsTables) -> list[str]:
    """Códigos cuyo nombre oficial NAICS COMPLETO aparece como tramo del título.

    Un nombre casa si cubre palabras consecutivas del tramo que empiezan y terminan en un límite
    (inicio/fin, coma o "and"), así "Accommodation" no casa dentro de "Accommodation and Food
    Services" y el nombre más largo gana. Los tramos no se solapan: un título que lista varios
    sectores devuelve todos los que casan, en orden de aparición."""
    out: list[tuple[int, int, str]] = []  # (tramo, posición, código)
    for si, seg in enumerate(NAME_SPLIT_RE.split(text or "")):
        words, comma = _tokens(seg)
        n = len(words)
        cands: list[tuple[int, int]] = []
        for i in range(n):
            if i and words[i - 1] != "and" and not comma[i - 1]:
                continue
            for j in range(i + 1, min(n, i + 40) + 1):
                if j < n and words[j] != "and" and not comma[j - 1]:
                    continue
                if " ".join(words[i:j]) in tables.by_name:
                    cands.append((i, j))
        taken = [False] * n
        for i, j in sorted(cands, key=lambda c: (-(c[1] - c[0]), c[0])):
            if any(taken[i:j]):
                continue
            for k in range(i, j):
                taken[k] = True
            out.append((si, i, tables.by_name[" ".join(words[i:j])]))
    codes: list[str] = []
    for _, _, code in sorted(out):
        if code not in codes:
            codes.append(code)
    return codes


def _valid_code(code: str, tables: NaicsTables) -> str | None:
    """`code` si es un NAICS 2022 oficial; los ceros de relleno a la derecha se recortan."""
    while len(code) >= 2:
        if code in tables.names:
            return code
        if code.endswith("0") and "-" not in code:
            code = code[:-1]
        else:
            return None
    return None


def census_codes(series_id: str, tables: NaicsTables) -> list[tuple[str, float]]:
    """(NAICS 2022, peso) de las dimensiones de industria de un id de Census (`col=valor`).

    Un código que ya es NAICS 2022 vale tal cual; uno de NAICS 2017 que cambió se pasa con la
    concordancia oficial (peso repartido). Un valor que no es NAICS (`TOTAL`, `44X72`) se omite."""
    acc: dict[str, float] = {}
    for part in (series_id or "").split(":"):
        col, sep, value = part.partition("=")
        if not sep or not CENSUS_COL_RE.match(col):
            continue
        value = value.strip()
        hit = _valid_code(value, tables)
        mapped = tables.to2022.get(value)
        if mapped and value not in tables.names:
            for code, w in mapped:
                acc[code] = acc.get(code, 0.0) + w
        elif hit:
            acc[hit] = acc.get(hit, 0.0) + 1.0
    total = sum(acc.values())
    return [(c, w / total) for c, w in sorted(acc.items())] if total else []


def bea_codes(series_id: str) -> list[str]:
    """Códigos de industria de BEA en el id (GDPbyIndustry, UnderlyingGDPbyIndustry, InputOutput).

    En InputOutput el id es `InputOutput:<tabla>:<RowCode>.<RowType>.<ColCode>.<ColType>:<freq>`
    (un código vacío se omite); se anclan los códigos marcados `Industry` y, si no hay, los de
    producto (los códigos de BEA son los mismos en ambos lados)."""
    m = BEA_INDUSTRY_RE.match(series_id or "")
    if m:
        return [m.group(1)]
    m = BEA_IO_RE.match(series_id or "")
    if not m:
        return []
    toks = m.group(1).split(".")
    sides: list[tuple[str, str]] = []  # (código, tipo)
    i = 0
    while i < len(toks):
        if toks[i] in ("Industry", "Commodity"):  # RowCode vacío: el tipo viene solo
            i += 1
        elif i + 1 < len(toks) and toks[i + 1] in ("Industry", "Commodity"):
            sides.append((toks[i], toks[i + 1]))
            i += 2
        else:
            i += 1
    industry = [c for c, t in sides if t == "Industry" and BEA_CODE_RE.match(c)]
    return industry or [c for c, _ in sides if BEA_CODE_RE.match(c)]


def bea_links(series_id: str, tables: NaicsTables) -> list[tuple[str, float]]:
    """(NAICS 2022, peso) del código BEA del id según la concordancia oficial de BEA."""
    codes = [c for c in bea_codes(series_id) if c in tables.bea]
    acc: dict[str, float] = defaultdict(float)
    for c in codes:
        for naics, w in tables.bea[c]:
            acc[naics] += w / len(codes)
    return sorted(acc.items())


def id_codes(source: str, series_id: str, tables: NaicsTables) -> list[str]:
    """NAICS embebido en el id propio de la serie (ver ID_FORMATS), validado contra la tabla."""
    for sources, pat in ID_FORMATS:
        m = pat.match(series_id or "") if source in sources else None
        if not m:
            continue
        code = m.group(1)
        while len(code) >= 2:
            if code in tables.names:
                return [code]
            if code.endswith("0") and "-" not in code:
                code = code[:-1]  # relleno de ceros a la derecha: 524100 -> 5241
            else:
                break
    return []


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
    """Código explícito en metadatos > código en el id > nombre oficial; peso uniforme."""
    out = {}
    for key, text in meta_text.items():
        codes, method = codes_in_text(text, tables), "codigo_en_metadatos"
        if not codes:
            codes, method = id_codes(key[0], key[1], tables), "codigo_en_id"
        if codes:
            out[key] = [(c, 1 / len(codes), method) for c in codes]
            continue
        weighted: list[tuple[str, float]] = []
        if key[0] == "census":
            weighted, method = census_codes(key[1], tables), "codigo_en_id"
        elif key[0] == "bea":
            weighted, method = bea_links(key[1], tables), "concordancia_bea"
        if weighted:
            out[key] = [(c, w, method) for c, w in weighted]
            continue
        codes = names_in_text(text, tables)
        if codes:
            out[key] = [(c, 1 / len(codes), "nombre_oficial") for c in codes]
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
    majority: float = 0.5,
) -> dict[tuple[str, str], list[Link]]:
    """`heredado_cluster`: los miembros sin NAICS reciben los códigos que reúnen MÁS de
    `majority` del peso de los miembros con código propio (cada miembro pesa 1, repartido entre
    sus códigos). Si ninguno alcanza la mayoría, el cluster no hereda nada."""
    out: dict[tuple[str, str], list[Link]] = {}
    for members in clusters:
        donors = [k for k in members if k in own]
        if not donors:
            continue
        acc: dict[str, float] = defaultdict(float)
        for k in donors:
            for naics, w, _ in own[k]:
                acc[naics] += w / len(donors)
        agreed = {n: w for n, w in acc.items() if w > majority}
        if not agreed:
            continue
        total = sum(agreed.values())
        links = [(n, w / total, "heredado_cluster") for n, w in sorted(agreed.items())]
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
