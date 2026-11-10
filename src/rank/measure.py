"""Clase de medida de cada serie: precio, cantidad, valor, tasa u otro (ADR 0014).

Un fenómeno cuyo respaldo son solo precios puede reflejar costos u oferta y no demanda. Para
saberlo hace falta decir QUÉ mide cada serie. La clase sale de metadatos de agencia, nunca de
temas: unidades ("Index", "Chained dollars", "Percent", "Thousands of persons"), códigos
oficiales de cada agencia (prefijos de encuesta de BLS) y el tipo oficial de tabla de BEA
("Price Indexes", "Chain-Type Quantity Indexes").

La tabla de reglas es `config/measure_kind.yaml` (versionada, con esquema validado aquí). Es una
tabla de metadatos de agencia y no una lista de temas: solo admite claves de unidades, prefijos de
códigos oficiales y descriptores de la medida; `tests/test_measure.py` verifica que ninguna
expresión nombre una industria ni un producto. Gana la primera regla que casa; sin regla, `otro`.

Subtipo: `costo_laboral` (salarios y compensación por hora, índice de costo laboral) es el precio
del trabajo: cuenta como `precio`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "config" / "measure_kind.yaml"
KINDS = ("precio", "cantidad", "valor", "tasa", "otro")
SUBTYPES = ("costo_laboral",)
# Una serie de cada clase cuenta como "demanda" si es cantidad o valor real; precio y tasa no.
DEMAND_KINDS = ("cantidad", "valor")
MATCH_KEYS = frozenset({"source", "id_regex", "units_regex", "title_regex"})
RULE_KEYS = frozenset({"id", "kind", "match"})


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    subtype: str | None
    match: Mapping[str, str]


@dataclass(frozen=True)
class Measure:
    kind: str
    subtype: str | None = None
    rule_id: str | None = None


def validate_table(data: Any) -> list[Rule]:
    """Valida el esquema de la tabla; lanza ValueError con todos los problemas."""
    errors: list[str] = []
    if not isinstance(data, dict) or set(data) - {"version", "rules"}:
        raise ValueError("measure_kind: solo se admiten las claves 'version' y 'rules'")
    if data.get("version") != 1:
        errors.append("version debe ser 1")
    rules_in = data.get("rules")
    if not isinstance(rules_in, list) or not rules_in:
        raise ValueError("measure_kind: 'rules' debe ser una lista no vacía")
    rules: list[Rule] = []
    seen: set[str] = set()
    for i, r in enumerate(rules_in):
        where = f"rules[{i}]"
        if not isinstance(r, dict) or not RULE_KEYS <= set(r) or set(r) - RULE_KEYS - {"subtype"}:
            errors.append(f"{where}: claves id, kind, match (y opcional subtype)")
            continue
        if not isinstance(r["id"], str) or not r["id"] or r["id"] in seen:
            errors.append(f"{where}: id vacío o repetido")
        seen.add(str(r["id"]))
        if r["kind"] not in KINDS:
            errors.append(f"{where}.kind debe ser uno de {KINDS}")
        sub = r.get("subtype")
        if sub is not None and (sub not in SUBTYPES or r["kind"] != "precio"):
            errors.append(f"{where}.subtype solo admite {SUBTYPES} y solo en kind precio")
        m = r["match"]
        if not isinstance(m, dict) or not m or set(m) - MATCH_KEYS:
            errors.append(f"{where}.match: claves permitidas {sorted(MATCH_KEYS)}")
            continue
        for k, v in m.items():
            if not isinstance(v, str) or not v:
                errors.append(f"{where}.match.{k} debe ser un texto no vacío")
            elif k != "source":
                try:
                    re.compile(v)
                except re.error:
                    errors.append(f"{where}.match.{k} no es una regex válida")
        rules.append(Rule(str(r["id"]), r["kind"], sub, m))
    if errors:
        raise ValueError("measure_kind inválida: " + "; ".join(errors))
    return rules


def load_rules(path: Path | None = None) -> list[Rule]:
    p = path or DEFAULT_PATH
    return validate_table(yaml.safe_load(p.read_text(encoding="utf-8")))


def classify(
    rules: Iterable[Rule],
    *,
    source: str,
    series_id: str,
    units: str = "",
    title: str = "",
) -> Measure:
    """Clase de una serie: la primera regla que casa con su fuente, id, unidades y título."""
    for r in rules:
        m = r.match
        if "source" in m and m["source"] != source:
            continue
        if "id_regex" in m and not re.search(m["id_regex"], series_id or ""):
            continue
        if "units_regex" in m and not re.search(m["units_regex"], units or ""):
            continue
        if "title_regex" in m and not re.search(m["title_regex"], title or ""):
            continue
        return Measure(r.kind, r.subtype, r.id)
    return Measure("otro")


def mix(kinds: Sequence[str | None]) -> dict[str, int]:
    """Cuántas series de cada clase; `None` (sin clasificar) cuenta como `otro`."""
    c = Counter(k or "otro" for k in kinds)
    return {k: c[k] for k in KINDS if c[k]}


def no_demand_evidence(kinds: Sequence[str | None]) -> bool | None:
    """True si TODO el respaldo numérico del fenómeno son precios (sin cantidad ni valor).

    None si no hay series o alguna no está clasificada (`otro`): con una serie desconocida no se
    puede afirmar que falte evidencia de demanda. Solo etiqueta: no filtra ni cambia el score."""
    if not kinds:
        return None
    ks = [k or "otro" for k in kinds]
    if "otro" in ks:
        return None
    return all(k == "precio" for k in ks)


def coverage(
    classified: Mapping[tuple[str, str], Measure],
) -> dict[str, dict[str, Any]]:
    """Por fuente: series, clasificadas (no `otro`), porcentaje y conteo por clase."""
    out: dict[str, dict[str, Any]] = {}
    for (src, _sid), m in sorted(classified.items()):
        d = out.setdefault(src, {"n": 0, "classified": 0, "by_kind": Counter()})
        d["n"] += 1
        d["classified"] += m.kind != "otro"
        d["by_kind"][m.kind] += 1
    return {
        s: {
            "n": d["n"],
            "classified": d["classified"],
            "pct": round(100 * d["classified"] / d["n"], 1),
            "by_kind": {k: d["by_kind"][k] for k in KINDS if d["by_kind"][k]},
        }
        for s, d in out.items()
    }
