"""Rol de cada serie frente a la industria: producción o insumo (ADR 0030, ajuste 2026-11-19).

El empleo, la nómina, los gastos, las horas y los salarios son INSUMOS (lo que la industria consume
para producir), no la cantidad producida. La tabla `config/measure_role.yaml` los separa de la
producción (ingresos, ventas, envíos) por la descripción oficial de la serie (título y unidades),
con el mismo esquema de `match` que `measure_kind.yaml`. Sin regla: `sin_clasificar`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.config import REPO_ROOT
from src.rank import measure

DEFAULT_PATH = REPO_ROOT / "config" / "measure_role.yaml"
ROLES = ("produccion", "insumo")
UNCLASSIFIED = "sin_clasificar"


def validate_table(data: Any) -> list[measure.Rule]:
    """Valida el esquema; lanza ValueError con todos los problemas. El rol va en `Rule.kind`."""
    errors: list[str] = []
    if not isinstance(data, dict) or set(data) - {"version", "rules"}:
        raise ValueError("measure_role: solo se admiten las claves 'version' y 'rules'")
    if data.get("version") != 1:
        errors.append("version debe ser 1")
    rules_in = data.get("rules")
    if not isinstance(rules_in, list) or not rules_in:
        raise ValueError("measure_role: 'rules' debe ser una lista no vacía")
    rules: list[measure.Rule] = []
    seen: set[str] = set()
    for i, r in enumerate(rules_in):
        where = f"rules[{i}]"
        if not isinstance(r, dict) or set(r) != {"id", "role", "match"}:
            errors.append(f"{where}: claves id, role, match")
            continue
        if not isinstance(r["id"], str) or not r["id"] or r["id"] in seen:
            errors.append(f"{where}: id vacío o repetido")
        seen.add(str(r["id"]))
        if r["role"] not in ROLES:
            errors.append(f"{where}.role debe ser uno de {ROLES}")
        m = r["match"]
        if not isinstance(m, dict) or not m or set(m) - measure.MATCH_KEYS:
            errors.append(f"{where}.match: claves permitidas {sorted(measure.MATCH_KEYS)}")
            continue
        rules.append(measure.Rule(str(r["id"]), str(r["role"]), None, m))
    if errors:
        raise ValueError("measure_role inválida: " + "; ".join(errors))
    return rules


def load_rules(path: Path | None = None) -> list[measure.Rule]:
    return validate_table(yaml.safe_load((path or DEFAULT_PATH).read_text(encoding="utf-8")))


def role_of(
    rules: list[measure.Rule], *, source: str, series_id: str, units: str = "", title: str = ""
) -> str:
    """`produccion`, `insumo` o `sin_clasificar` (primera regla que casa)."""
    m = measure.classify(rules, source=source, series_id=series_id, units=units, title=title)
    return m.kind if m.rule_id else UNCLASSIFIED
