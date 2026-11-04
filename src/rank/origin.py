"""Origen del dato y duplicados entre agencias (principio 4, ADR 0010).

La independencia se cuenta por ORIGEN: quién produjo el dato primario, no quién lo publica.
- `config/data_origin.yaml`: tabla versionada de republicaciones conocidas (esquema validado aquí).
- Detector de duplicados: dos series de agencias distintas con correlación de crecimiento muy
  alta y nivel casi igual se marcan "posible mismo origen" (revisión humana) y cuentan como una
  sola fuente.
Una serie sin regla ni duplicado conserva su agencia como origen.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from src.config import REPO_ROOT
from src.rank.agencies import agency_key

MATCH_KEYS = frozenset({"source", "release_id", "agency", "series_id_regex", "units_regex"})
DEFAULT_PATH = REPO_ROOT / "config" / "data_origin.yaml"


@dataclass(frozen=True)
class Rule:
    id: str
    match: Mapping[str, Any]
    origin: str
    chain: tuple[str, ...]


@dataclass(frozen=True)
class Origin:
    keys: frozenset[str]  # identidad para la triangulación
    chain: tuple[str, ...]  # raíz primero; vacío si no hay regla
    rule_id: str | None = None


def validate_table(data: Any) -> list[Rule]:
    """Valida el esquema de la tabla; lanza ValueError con todos los problemas."""
    errors: list[str] = []
    if not isinstance(data, dict) or set(data) - {"version", "rules"}:
        raise ValueError("data_origin: solo se admiten las claves 'version' y 'rules'")
    if data.get("version") != 1:
        errors.append("version debe ser 1")
    rules_in = data.get("rules")
    if not isinstance(rules_in, list) or not rules_in:
        raise ValueError("data_origin: 'rules' debe ser una lista no vacía")
    rules, seen = [], set()
    for i, r in enumerate(rules_in):
        where = f"rules[{i}]"
        if not isinstance(r, dict) or set(r) != {"id", "match", "origin", "chain"}:
            errors.append(f"{where}: claves exactas id, match, origin, chain")
            continue
        if not isinstance(r["id"], str) or not r["id"] or r["id"] in seen:
            errors.append(f"{where}: id vacío o repetido")
        seen.add(r["id"])
        m = r["match"]
        if not isinstance(m, dict) or not m or set(m) - MATCH_KEYS:
            errors.append(f"{where}.match: claves permitidas {sorted(MATCH_KEYS)}")
            continue
        if "release_id" in m and not isinstance(m["release_id"], int):
            errors.append(f"{where}.match.release_id debe ser entero")
        for k in ("series_id_regex", "units_regex"):
            if k in m:
                try:
                    re.compile(m[k])
                except (re.error, TypeError):
                    errors.append(f"{where}.match.{k} no es una regex válida")
        if not isinstance(r["origin"], str) or not agency_key(r["origin"]):
            errors.append(f"{where}.origin debe ser un texto no vacío")
        chain = r["chain"]
        if (
            not isinstance(chain, list)
            or len(chain) < 2
            or not all(isinstance(c, str) for c in chain)
        ):
            errors.append(f"{where}.chain: lista de ≥ 2 textos (raíz primero)")
            continue
        rules.append(Rule(r["id"], m, agency_key(r["origin"]), tuple(chain)))
    if errors:
        raise ValueError("data_origin inválida: " + "; ".join(errors))
    return rules


def load_rules(path: Path | None = None) -> list[Rule]:
    p = path or DEFAULT_PATH
    return validate_table(yaml.safe_load(p.read_text(encoding="utf-8")))


def resolve_origin(
    rules: Iterable[Rule],
    *,
    source: str,
    series_id: str,
    agencies: frozenset[str],
    release_ids: Iterable[int] = (),
    units: str = "",
) -> Origin:
    """Origen de una serie: la primera regla que casa; si no, sus agencias."""
    rels = set(release_ids)
    for r in rules:
        m = r.match
        if "source" in m and m["source"] != source:
            continue
        if "release_id" in m and m["release_id"] not in rels:
            continue
        if "agency" in m and agency_key(m["agency"]) not in agencies:
            continue
        if "series_id_regex" in m and not re.search(m["series_id_regex"], series_id):
            continue
        if "units_regex" in m and not re.search(m["units_regex"], units or ""):
            continue
        return Origin(frozenset({r.origin}), r.chain, r.id)
    return Origin(agencies, ())


def merge_identities(
    identities: dict[tuple[str, str], frozenset[str]],
    pairs: Iterable[tuple[tuple[str, str], tuple[str, str], float]],
) -> None:
    """Las series de un par duplicado (y sus encadenados) pasan a compartir identidad."""
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(x: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ in pairs:
        parent[find(a)] = find(b)
    comps: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for k in list(parent):
        comps.setdefault(find(k), []).append(k)
    for members in comps.values():
        union = frozenset().union(*(identities[m] for m in members))
        for m in members:
            identities[m] = union


def format_chain(chain: Iterable[str]) -> str:
    """ "BLS → republicado por OCDE MEI" (raíz → quien republica)."""
    c = list(chain)
    if len(c) < 2:
        return ""
    return c[0] + "".join(f" → republicado por {x}" for x in c[1:])


@dataclass
class DuplicateParams:
    rho: float
    level_tol: float
    min_overlap: int


def level_close(
    a: Mapping[int, float], b: Mapping[int, float], tol: float, min_overlap: int
) -> bool:
    """Nivel casi igual: la mediana de |a−b| / |media| en los periodos comunes ≤ tol."""
    common = sorted(a.keys() & b.keys())
    if len(common) < min_overlap:
        return False
    x = np.array([a[i] for i in common], float)
    y = np.array([b[i] for i in common], float)
    scale = np.maximum((np.abs(x) + np.abs(y)) / 2, 1e-12)
    return bool(np.median(np.abs(x - y) / scale) <= tol)


def find_duplicates(
    keys: list[tuple[str, str]],
    rho: np.ndarray,
    levels: Mapping[tuple[str, str], tuple[str, Mapping[int, float]]],
    identities: Mapping[tuple[str, str], frozenset[str]],
    p: DuplicateParams,
) -> list[tuple[tuple[str, str], tuple[str, str], float]]:
    """Pares de series con identidades distintas, ρ de crecimientos > p.rho y nivel casi igual."""
    out = []
    n = len(keys)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = keys[i], keys[j]
            if identities[a] & identities[b] or not (identities[a] and identities[b]):
                continue
            r = rho[i, j]
            if not np.isfinite(r) or r <= p.rho:
                continue
            if a not in levels or b not in levels or levels[a][0] != levels[b][0]:
                continue  # el nivel se compara solo en la misma grilla
            if level_close(levels[a][1], levels[b][1], p.level_tol, p.min_overlap):
                out.append((a, b, float(r)))
    return out
