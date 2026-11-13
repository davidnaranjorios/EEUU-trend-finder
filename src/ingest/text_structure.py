# ruff: noqa: E501  (consultas SQL y textos de informe en una línea)
"""Inventario de la estructura de los documentos de texto (ADR 0026, Etapa 1 del plan).

Solo DESCRIBE: lee `processed/text_structure/` y escribe `docs/estructura-texto.md` con la cobertura
de cada campo por año, las secciones (Items) de EDGAR por año, las empresas por industria y año y
los cambios de formato. No toca detectores ni `term_freq`. Los umbrales de lectura (cuándo «nace»
una sección, qué salto de cobertura es un cambio de formato) están en `text.structure` de
`config.yaml`; ninguna lista de industrias, temas o palabras vive aquí.

Uso: `uv run python -m src.ingest.text structure-report [--processed-dir D] [--out F]`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

EDGAR_FIELDS = (
    "sic",
    "state_incorporation",
    "state_business",
    "fiscal_year_end",
    "filer_category",
    "items_json",
    "facts_n_concepts",
)
FR_FIELDS = (
    "doc_type",
    "agency",
    "subagency",
    "cfr_json",
    "topics_json",
    "significant",
    "format_json",
)


def md_table(header: list[str], rows: list[list[Any]]) -> str:
    """Tabla Markdown; None -> «—»."""
    cell = lambda v: "—" if v is None else str(v)  # noqa: E731
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def _connect(processed: Path) -> duckdb.DuckDBPyConnection | None:
    files = sorted((processed / "text_structure").glob("*/*/*.parquet"))
    if not files:
        return None
    con = duckdb.connect()
    listing = ", ".join("'" + str(f).replace("'", "''") + "'" for f in files)
    con.execute(
        f"CREATE VIEW ts AS SELECT *, year(date) AS year "
        f"FROM read_parquet([{listing}], union_by_name=true)"
    )
    return con


def _years(con: duckdb.DuckDBPyConnection, source: str) -> list[int]:
    return [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT year FROM ts WHERE source=? ORDER BY 1", [source]
        ).fetchall()
    ]


def coverage(con: duckdb.DuckDBPyConnection, source: str, fields: tuple[str, ...]) -> str:
    """% de documentos con el campo (no nulo y, para listas, no vacío) por año."""
    years = _years(con, source)
    rows = []
    for f in fields:
        nonempty = f"{f} IS NOT NULL AND {f}::VARCHAR NOT IN ('[]', '{{}}', '')"
        cols = ", ".join(
            f"round(100.0 * count(*) FILTER (WHERE year={y} AND ({nonempty})) "
            f"/ nullif(count(*) FILTER (WHERE year={y}), 0), 0)::INT"
            for y in years
        )
        rows.append(
            [f"`{f}`", *con.execute(f"SELECT {cols} FROM ts WHERE source=?", [source]).fetchone()]
        )
    n = con.execute(
        "SELECT "
        + ", ".join(f"count(*) FILTER (WHERE year={y})" for y in years)
        + " FROM ts WHERE source=?",
        [source],
    ).fetchone()
    rows.append(["**documentos**", *n])
    return md_table(["campo (% de documentos)", *map(str, years)], rows)


def items_by_year(con: duckdb.DuckDBPyConnection, birth_min_share: float) -> tuple[str, str]:
    """Share de documentos con cada Item por año, su largo mediano y el año en que «nace»."""
    rows = con.execute(
        "SELECT year, doc_id, je.key AS item, je.value::BIGINT AS n "
        "FROM ts, json_each(items_json::JSON) AS je WHERE source='edgar' AND items_json IS NOT NULL"
    ).fetchall()
    docs = dict(
        con.execute(
            "SELECT year, count(*) FROM ts WHERE source='edgar' AND items_json IS NOT NULL GROUP BY 1"
        ).fetchall()
    )
    years = sorted(docs)
    by_item: dict[str, dict[int, list[int]]] = {}
    for y, _doc, item, n in rows:
        by_item.setdefault(item, {}).setdefault(y, []).append(n)

    def order(k: str) -> tuple[int, str]:
        digits = "".join(c for c in k if c.isdigit())
        return (int(digits) if digits else 99, k)

    table, births = [], []
    for item in sorted(by_item, key=order):
        shares = {y: len(by_item[item].get(y, [])) / docs[y] for y in years}
        if max(shares.values()) < 0.01:
            continue  # ruido de encabezados falsos
        table.append([f"Item {item}", *[f"{round(100 * shares[y])}%" for y in years]])
        first = next((y for y in years if shares[y] >= birth_min_share), None)
        sizes = [v for ns in by_item[item].values() for v in ns]
        sizes.sort()
        births.append([f"Item {item}", first, sizes[len(sizes) // 2] if sizes else None])
    return (
        md_table(["Item (% de 10-K que lo trae)", *map(str, years)], table),
        md_table(
            [
                "Item",
                f"primer año con ≥ {round(100 * birth_min_share)}% de los 10-K",
                "largo mediano (caracteres)",
            ],
            births,
        ),
    )


def firms_by_industry(con: duckdb.DuckDBPyConnection, thresholds: list[int]) -> str:
    """Cuántas industrias SIC (4 y 2 dígitos) tienen al menos N empresas distintas, por año."""
    out = []
    for digits in (4, 2):
        years = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT year FROM ts WHERE source='edgar' AND sic IS NOT NULL ORDER BY 1"
            ).fetchall()
        ]
        rows = []
        for y in years:
            per = con.execute(
                "SELECT left(sic, ?) AS g, count(DISTINCT entity) AS n FROM ts "
                "WHERE source='edgar' AND year=? AND sic IS NOT NULL GROUP BY 1",
                [digits, y],
            ).fetchall()
            ns = sorted(n for _, n in per)
            med = ns[len(ns) // 2] if ns else None
            rows.append([y, len(per), sum(ns), med, *[sum(n >= t for n in ns) for t in thresholds]])
        out.append(
            f"**SIC de {digits} dígitos** (empresas distintas en la muestra):\n\n"
            + md_table(
                [
                    "año",
                    "industrias",
                    "empresas",
                    "mediana por industria",
                    *[f"≥ {t} empresas" for t in thresholds],
                ],
                rows,
            )
        )
    return "\n\n".join(out)


def fr_types_by_year(con: duckdb.DuckDBPyConnection) -> str:
    years = _years(con, "federal_register")
    types = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT doc_type FROM ts WHERE source='federal_register' AND doc_type IS NOT NULL ORDER BY 1"
        ).fetchall()
    ]
    rows = [
        [
            f"`{t}`",
            *[
                con.execute(
                    "SELECT count(*) FROM ts WHERE source='federal_register' AND doc_type=? AND year=?",
                    [t, y],
                ).fetchone()[0]
                for y in years
            ],
        ]
        for t in types
    ]
    rows.append(
        [
            "agencias distintas",
            *[
                con.execute(
                    "SELECT count(DISTINCT agency) FROM ts WHERE source='federal_register' AND year=?",
                    [y],
                ).fetchone()[0]
                for y in years
            ],
        ]
    )
    rows.append(
        [
            "subagencias distintas",
            *[
                con.execute(
                    "SELECT count(DISTINCT subagency) FROM ts WHERE source='federal_register' AND year=?",
                    [y],
                ).fetchone()[0]
                for y in years
            ],
        ]
    )
    return md_table(["documentos en la muestra", *map(str, years)], rows)


def format_changes(con: duckdb.DuckDBPyConnection, jump: float) -> str:
    """Huella de formato por año: cobertura de cada etiqueta del encabezado (FR) y del tipo de
    archivo (EDGAR); se marcan los años en que la cobertura cambia ≥ `jump` respecto del anterior."""
    lines: list[str] = []
    for source, label in (
        ("federal_register", "Federal Register (XML de govinfo)"),
        ("edgar", "EDGAR (10-K)"),
    ):
        years = _years(con, source)
        feats: dict[str, dict[int, float]] = {}
        docs = dict(
            con.execute(
                "SELECT year, count(*) FROM ts WHERE source=? AND format_json IS NOT NULL GROUP BY 1",
                [source],
            ).fetchall()
        )
        for y, fj in con.execute(
            "SELECT year, format_json FROM ts WHERE source=? AND format_json IS NOT NULL", [source]
        ).fetchall():
            fmt = json.loads(fj)
            toks = [
                f"{k}:{t}"
                for k, v in fmt.items()
                for t in (v if isinstance(v, list) else [v] if v not in (False, None) else [])
            ]
            for t in set(toks):
                feats.setdefault(t, {}).setdefault(y, 0)
                feats[t][y] += 1
        rows, changes = [], []
        for t in sorted(feats):
            share = {y: feats[t].get(y, 0) / docs[y] for y in years if y in docs}
            ys = [y for y in years if y in docs]
            rows.append([f"`{t}`", *[f"{round(100 * share[y])}%" for y in ys]])
            for a, b in zip(ys, ys[1:], strict=False):
                if abs(share[b] - share[a]) >= jump:
                    changes.append(
                        f"- `{t}`: {round(100 * share[a])}% en {a} → {round(100 * share[b])}% en {b}"
                    )
        ys = [y for y in years if y in docs]
        lines.append(
            f"### {label}\n\n" + md_table(["huella (% de documentos)", *map(str, ys)], rows)
        )
        lines.append(
            f"\nCambios de ≥ {round(100 * jump)} puntos de un año al siguiente:\n\n"
            + ("\n".join(changes) or "- ninguno")
        )
    return "\n\n".join(lines)


def build_report(processed: Path, cfg: dict[str, Any], today: date | None = None) -> str:
    con = _connect(processed)
    if con is None:
        raise SystemExit(f"sin datos en {processed}/text_structure: corre la ingesta primero")
    sc = cfg["text"]["structure"]
    n = {
        s: con.execute("SELECT count(*) FROM ts WHERE source=?", [s]).fetchone()[0]
        for s in ("edgar", "federal_register")
    }
    parts = [
        "# Estructura de los documentos de texto (Etapa 1)",
        f"Generado el {today or date.today()} por `uv run python -m src.ingest.text structure-report` "
        f"(ADR 0026). Documentos en esta tabla: EDGAR {n['edgar']}, Federal Register {n['federal_register']}. "
        "**Solo describe**: ningún detector ni `term_freq` cambió. Si la tabla viene de una muestra "
        "(`--sample`), los conteos de empresas son de la muestra, no del universo.",
        "## 1. Reportes anuales de empresas (EDGAR, 10-K)",
        "Año = año de presentación. Los metadatos del emisor (industria, estado, cierre fiscal, "
        "categoría) salen de `submissions` en su estado ACTUAL: no son el valor histórico del "
        "momento de la presentación.",
        "### 1.1 Cobertura por campo y año\n\n" + coverage(con, "edgar", EDGAR_FIELDS),
    ]
    t_items, t_births = items_by_year(con, float(sc["birth_min_share"]))
    parts += [
        "### 1.2 Secciones (Items) por año\n\n" + t_items,
        "### 1.3 Cuándo nacen las secciones\n\n" + t_births,
        "### 1.4 Empresas por industria y año\n\n"
        + firms_by_industry(con, list(sc["firm_thresholds"])),
        "## 2. Registro Federal",
        "### 2.1 Cobertura por campo y año\n\n" + coverage(con, "federal_register", FR_FIELDS),
        "### 2.2 Documentos por tipo, agencias y subagencias\n\n" + fr_types_by_year(con),
        "## 3. Cambios de formato por año\n\n" + format_changes(con, float(sc["format_jump"])),
        "## 4. Datos financieros estructurados de la SEC (companyfacts)\n\n"
        "`facts_n_concepts` (tabla 1.1) cuenta, para los 10-K sondeados, los conceptos XBRL con algún "
        "hecho reportado en esa presentación (`accn` igual al número de acceso). **Solo cobertura**: "
        "no se ingieren los valores. Vacío = no sondeado o sin XBRL.",
    ]
    return "\n\n".join(parts) + "\n"


def write_report(processed: Path, out: Path, cfg: dict[str, Any]) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report(processed, cfg), encoding="utf-8")
    return out
