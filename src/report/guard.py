"""Segunda barrera de copyright: nada del sitio publicado puede nombrar una serie con copyright.

La primera barrera está al armar los datos (src.report.data filtra `copyrighted = true`). Esta
recorre cada archivo de `site/` antes de publicar:
- extrae los tokens con forma de identificador y todas las cadenas de texto (valores JSON, nodos
  de texto y atributos HTML);
- falla si alguno coincide con el id o el título de una serie con copyright.

Un id o título que también tiene una serie pública de otra fuente no se prohíbe (sería ambiguo:
la serie pública sí puede aparecer).
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_TOKEN = re.compile(r"[A-Za-z0-9_.\-]+")
_TEXT_NODE = re.compile(r">([^<]+)<")
_ATTR = re.compile(r'="([^"]*)"')
SCANNED_SUFFIXES = (".html", ".json", ".csv", ".svg", ".js")


class CopyrightLeak(RuntimeError):
    """El sitio contiene el id o el título de una serie con copyright."""


def normalize_text(s: str) -> str:
    return " ".join(html.unescape(s).casefold().split())


def forbidden_sets(
    copyrighted: Iterable[tuple[str, str | None]], public: Iterable[tuple[str, str | None]]
) -> tuple[set[str], set[str]]:
    """(ids, títulos normalizados) prohibidos, descontando los que también son públicos."""
    pub_ids, pub_titles = set(), set()
    for sid, title in public:
        pub_ids.add(sid)
        if title:
            pub_titles.add(normalize_text(title))
    ids, titles = set(), set()
    for sid, title in copyrighted:
        if sid not in pub_ids:
            ids.add(sid)
        if title and normalize_text(title) not in pub_titles:
            titles.add(normalize_text(title))
    return ids, titles


def _json_strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _json_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_strings(v)


def file_strings(path: Path) -> set[str]:
    text = path.read_text("utf-8", errors="replace")
    strings: set[str] = set()
    if path.suffix == ".json":
        try:
            strings.update(_json_strings(json.loads(text)))
        except json.JSONDecodeError:
            strings.add(text)
    else:
        strings.update(m.group(1) for m in _TEXT_NODE.finditer(text))
        strings.update(m.group(1) for m in _ATTR.finditer(text))
        for m in _ATTR.finditer(text):  # data-* con JSON (puntos de los gráficos)
            raw = html.unescape(m.group(1))
            if raw.startswith(("{", "[")):
                try:
                    strings.update(_json_strings(json.loads(raw)))
                except json.JSONDecodeError:
                    pass
    return strings


def scan(site_dir: Path, ids: set[str], titles: set[str]) -> list[str]:
    """Hallazgos "archivo: id/título" (vacío si el sitio está limpio)."""
    found: list[str] = []
    for path in sorted(p for p in Path(site_dir).rglob("*") if p.is_file()):
        if path.suffix not in SCANNED_SUFFIXES:
            continue
        strings = file_strings(path)
        tokens = {t for s in strings for t in _TOKEN.findall(s)}
        norm = {normalize_text(s) for s in strings}
        rel = path.relative_to(site_dir)
        found += [f"{rel}: id {t}" for t in sorted(tokens & ids)]
        found += [f"{rel}: título {t!r}" for t in sorted(norm & titles)]
        # Títulos dentro de cadenas más largas (p. ej. "Título | unidades") en HTML; en JSON
        # (fichas del explorador) cada título es un valor propio y basta la coincidencia exacta.
        if titles and path.suffix != ".json":
            for s in norm - titles:
                for t in titles:
                    if len(t) >= 12 and t in s:
                        found.append(f"{rel}: título {t!r} dentro de {s[:60]!r}")
    return found


def assert_publishable(site_dir: Path, ids: set[str], titles: set[str]) -> None:
    found = scan(site_dir, ids, titles)
    if found:
        head = "\n".join(found[:20])
        raise CopyrightLeak(f"{len(found)} referencias a series con copyright:\n{head}")
