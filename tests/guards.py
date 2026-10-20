"""Escáneres estáticos de los principios 1 y 2. Viven en tests/, no en src/."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

# --- Principio 2: src/ no toca eval/ ---------------------------------------------------------

EVAL_PATH_RE = re.compile(r"(^|[\\/])eval([\\/]|$)|known_events")
IMPORT_FUNCS = {"import_module", "__import__", "find_spec", "spec_from_file_location"}


def eval_access_violations(root: Path) -> list[str]:
    """Lista de accesos a eval/ desde los archivos bajo `root`."""
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix == ".py":
            out.extend(f"{path}: {v}" for v in _py_eval_violations(path))
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if EVAL_PATH_RE.search(text):
                out.append(f"{path}: referencia a eval/ en archivo no Python")
    return out


def _py_eval_violations(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "eval" or alias.name.startswith("eval."):
                    yield f"línea {node.lineno}: import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and (mod == "eval" or mod.startswith("eval.")):
                yield f"línea {node.lineno}: from {mod} import ..."
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "eval" or EVAL_PATH_RE.search(node.value):
                yield f"línea {node.lineno}: literal {node.value!r}"


# --- Principio 1: cero temas hardcodeados -----------------------------------------------------

MAX_STRING_LITERALS = 5
# Forma típica de un ID de FRED/BLS/BEA: mayúsculas y dígitos, al menos una letra y un dígito.
SERIES_ID_RE = re.compile(r"^(?=[A-Z0-9_]*[A-Z])(?=[A-Z0-9_]*[0-9])[A-Z0-9_]{5,}$")
# Canarios: IDs conocidos que, sin dígito, escapan a SERIES_ID_RE.
CANARY_SERIES_IDS = {"UNRATE", "CPIAUCSL", "PAYEMS", "INDPRO", "FEDFUNDS", "HOUST", "RSAFS"}
TOPIC_KEY_RE = re.compile(
    r"topic|keyword|term|industr|sector|watch|interest|series_id|trend", re.IGNORECASE
)


def _looks_like_series_id(value: str) -> bool:
    return bool(SERIES_ID_RE.match(value)) or value in CANARY_SERIES_IDS


def hardcoded_topic_violations_py(root: Path) -> list[str]:
    """Listas largas de strings literales o IDs de serie en los .py bajo `root`."""
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.List | ast.Tuple | ast.Set):
                n = _count_str_constants(node.elts)
                if n > MAX_STRING_LITERALS:
                    out.append(f"{path}:{node.lineno}: colección literal con {n} strings")
            elif isinstance(node, ast.Dict):
                # Mapeos literales (clave string -> constante, o valores string). Un dict con
                # claves string y valores calculados, como un to_dict(), no es una lista de temas.
                n = max(
                    sum(
                        isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                        and isinstance(v, ast.Constant)
                        for k, v in zip(node.keys, node.values, strict=True)
                    ),
                    _count_str_constants(node.values),
                )
                if n > MAX_STRING_LITERALS:
                    out.append(f"{path}:{node.lineno}: dict literal con {n} strings")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _looks_like_series_id(node.value):
                    out.append(f"{path}:{node.lineno}: posible ID de serie {node.value!r}")
    return out


def _count_str_constants(nodes: list[ast.expr | None]) -> int:
    return sum(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in nodes)


def hardcoded_topic_violations_yaml(path: Path) -> list[str]:
    """Claves temáticas, listas largas de strings o IDs de serie en un YAML."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [f"{path}: {v}" for v in _walk_yaml(data, "")]


def _walk_yaml(node: Any, where: str) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{where}.{key}" if where else str(key)
            if TOPIC_KEY_RE.search(str(key)):
                yield f"clave temática '{here}'"
            yield from _walk_yaml(value, here)
    elif isinstance(node, list):
        n = sum(isinstance(v, str) for v in node)
        if n > MAX_STRING_LITERALS:
            yield f"lista de {n} strings en '{where}'"
        for i, value in enumerate(node):
            yield from _walk_yaml(value, f"{where}[{i}]")
    elif isinstance(node, str) and _looks_like_series_id(node):
        yield f"posible ID de serie {node!r} en '{where}'"
