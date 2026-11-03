"""Esquemas comunes (ver CLAUDE.md, "Esquemas comunes") y su validación."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pyarrow as pa

from src.config import DOWNLOADABLE_GROUPS, FREQUENCIES


class SchemaError(ValueError):
    """Una tabla no cumple su esquema común."""


@dataclass(frozen=True)
class TableSpec:
    name: str
    schema: pa.Schema
    key: tuple[str, ...]


def _schema(*fields: tuple[str, pa.DataType, bool]) -> pa.Schema:
    return pa.schema([pa.field(name, dtype, nullable=nullable) for name, dtype, nullable in fields])


SERIES = TableSpec(
    name="series",
    schema=_schema(
        ("source", pa.string(), False),
        ("series_id", pa.string(), False),
        ("date", pa.date32(), False),
        ("value", pa.float64(), True),
        ("vintage_date", pa.date32(), True),
        ("frequency", pa.string(), False),
        ("units", pa.string(), True),
        ("seasonal_adjustment", pa.string(), True),
    ),
    key=("source", "series_id", "date", "vintage_date"),
)

SERIES_META = TableSpec(
    name="series_meta",
    schema=_schema(
        ("source", pa.string(), False),
        ("series_id", pa.string(), False),
        ("title", pa.string(), True),
        ("category_path", pa.string(), True),
        ("start_date", pa.date32(), True),
        ("end_date", pa.date32(), True),
        ("last_updated", pa.timestamp("s", tz="UTC"), True),
        ("discontinued", pa.bool_(), False),
        # Serie de terceros con copyright: se analiza, pero no se publica en el reporte.
        ("copyrighted", pa.bool_(), False),
    ),
    key=("source", "series_id"),
)

TEXT_DOCS = TableSpec(
    name="text_docs",
    schema=_schema(
        ("source", pa.string(), False),
        ("doc_id", pa.string(), False),
        ("date", pa.date32(), False),
        ("entity", pa.string(), True),
        ("section", pa.string(), False),
        ("text_hash", pa.string(), False),
    ),
    key=("source", "doc_id", "section"),
)

# `section` separa Item 1A / Item 7 (EDGAR) o el tipo de documento (Federal Register) sin
# convertirlos en fuentes distintas: para la triangulación, la fuente es `source`.
TERM_FREQ = TableSpec(
    name="term_freq",
    schema=_schema(
        ("source", pa.string(), False),
        ("section", pa.string(), False),
        ("period", pa.string(), False),
        ("term", pa.string(), False),
        ("doc_count", pa.int64(), False),
        ("total_docs", pa.int64(), False),
    ),
    key=("source", "section", "period", "term"),
)

SIGNALS = TableSpec(
    name="signals",
    schema=_schema(
        ("signal_id", pa.string(), False),
        ("source", pa.string(), False),
        ("object_id", pa.string(), False),
        ("detector", pa.string(), False),
        ("window_start", pa.date32(), False),
        ("window_end", pa.date32(), False),
        ("effect_size", pa.float64(), False),
        ("p_value", pa.float64(), False),
        ("q_value", pa.float64(), False),
        ("run_date", pa.date32(), False),
        ("run_id", pa.string(), False),
        # Número de pruebas de la familia contra las que se corrigió (BH).
        ("n_tests", pa.int64(), False),
    ),
    key=("signal_id",),
)

# --- Linaje (ADR 0002): toda decisión queda registrada con el run_id que la tomó. ---------------

# Grupos y reglas del embudo de elegibilidad (src/eligibility.py).
# Los grupos descargables más los dos que no se descargan: sin geografía y excluidas.
ELIGIBILITY_GROUPS = (*DOWNLOADABLE_GROUPS, "sin_geografia", "excluida")

ELIGIBILITY = TableSpec(
    name="eligibility",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("source", pa.string(), False),
        ("series_id", pa.string(), False),
        ("group_name", pa.string(), False),
        ("rule", pa.string(), False),
        ("detail", pa.string(), True),
        ("geo_level", pa.string(), True),
        ("frequency", pa.string(), True),
        ("copyrighted", pa.bool_(), False),
        ("decided_at", pa.timestamp("s", tz="UTC"), False),
    ),
    key=("run_id", "source", "series_id"),
)

# Resultado de pedir el historial ALFRED de cada serie de la muestra de vintages (ADR 0002).
# Una fila por serie intentada en cada corrida; el motivo vigente es el de la última corrida.
VINTAGE_STATUSES = ("ok", "no_existe_en_alfred", "error_api")
VINTAGE_LINEAGE = TableSpec(
    name="vintage_lineage",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("source", pa.string(), False),
        ("series_id", pa.string(), False),
        ("status", pa.string(), False),
        ("n_rows", pa.int64(), False),
        ("detail", pa.string(), True),
        ("decided_at", pa.timestamp("s", tz="UTC"), False),
    ),
    key=("run_id", "source", "series_id"),
)

DETECTOR_RUNS = TableSpec(
    name="detector_runs",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("detector", pa.string(), False),
        ("started_at", pa.timestamp("s", tz="UTC"), False),
        ("finished_at", pa.timestamp("s", tz="UTC"), True),
        ("n_tests", pa.int64(), False),
        ("n_bh", pa.int64(), False),
        ("n_by", pa.int64(), False),
        ("q", pa.float64(), False),
        ("params_json", pa.string(), False),
    ),
    key=("run_id", "detector"),
)

SERIES_TERM_LINKS = TableSpec(
    name="series_term_links",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("source", pa.string(), False),
        ("series_id", pa.string(), False),
        ("text_source", pa.string(), False),
        ("term", pa.string(), False),
        ("score", pa.float64(), False),
        ("method", pa.string(), False),
    ),
    key=("run_id", "source", "series_id", "text_source", "term"),
)

# Estados del linaje de texto: por qué cada documento/sección entró o no a las frecuencias.
# `excluido` lleva el motivo en `detail` (formulario fuera de alcance, error de descarga...).
TEXT_INGEST_STATUSES = ("ok", "sin_seccion", "muy_corta", "duplicado", "excluido")

# Una fila por documento y sección de cada periodo procesado (la partición del periodo se
# reemplaza completa al reprocesarlo). `section = "*"` marca decisiones sobre el documento entero.
TEXT_INGEST = TableSpec(
    name="text_ingest",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("source", pa.string(), False),
        ("doc_id", pa.string(), False),
        ("section", pa.string(), False),
        ("period", pa.string(), False),
        ("status", pa.string(), False),
        ("detail", pa.string(), True),
        ("n_chars", pa.int64(), True),
        ("decided_at", pa.timestamp("s", tz="UTC"), False),
    ),
    key=("run_id", "source", "doc_id", "section"),
)

CLUSTERS = TableSpec(
    name="clusters",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("cluster_id", pa.string(), False),
        ("source", pa.string(), False),
        ("object_id", pa.string(), False),
        ("role", pa.string(), False),  # representante o miembro
        ("similarity", pa.float64(), True),
    ),
    key=("run_id", "cluster_id", "source", "object_id"),
)

ALL_SPECS = {
    spec.name: spec
    for spec in (
        SERIES,
        SERIES_META,
        TEXT_DOCS,
        TERM_FREQ,
        SIGNALS,
        ELIGIBILITY,
        VINTAGE_LINEAGE,
        DETECTOR_RUNS,
        SERIES_TERM_LINKS,
        CLUSTERS,
        TEXT_INGEST,
    )
}


def validate(table: pa.Table, spec: TableSpec) -> None:
    """Lanza SchemaError si `table` no cumple columnas, tipos, nulos, dominios o clave única."""
    expected = {f.name: f for f in spec.schema}
    actual = {f.name: f for f in table.schema}

    missing = expected.keys() - actual.keys()
    extra = actual.keys() - expected.keys()
    if missing or extra:
        raise SchemaError(
            f"{spec.name}: columnas faltantes {sorted(missing)}, sobrantes {sorted(extra)}"
        )

    for name, field in expected.items():
        if not actual[name].type.equals(field.type):
            raise SchemaError(
                f"{spec.name}.{name}: tipo {actual[name].type}, se esperaba {field.type}"
            )
        if not field.nullable and table.column(name).null_count > 0:
            raise SchemaError(f"{spec.name}.{name}: contiene nulos y no es nullable")

    _check_domains(table, spec)
    _check_lineage(table, spec)
    _check_unique_key(table, spec)


def _check_domains(table: pa.Table, spec: TableSpec) -> None:
    con = duckdb.connect()
    con.register("t", table)
    if "frequency" in expected_columns(spec):
        allowed = ", ".join(f"'{f}'" for f in FREQUENCIES)
        bad = con.execute(f"SELECT count(*) FROM t WHERE frequency NOT IN ({allowed})").fetchone()
        if bad[0]:
            raise SchemaError(f"{spec.name}.frequency: {bad[0]} filas fuera de {FREQUENCIES}")
    if spec is TERM_FREQ:
        bad = con.execute(
            "SELECT count(*) FROM t WHERE doc_count < 0 OR total_docs <= 0 "
            "OR doc_count > total_docs"
        ).fetchone()
        if bad[0]:
            raise SchemaError(f"term_freq: {bad[0]} filas con doc_count/total_docs inconsistentes")
    if spec is SIGNALS:
        bad = con.execute(
            "SELECT count(*) FROM t WHERE p_value NOT BETWEEN 0 AND 1 "
            "OR q_value NOT BETWEEN 0 AND 1 OR window_start > window_end"
        ).fetchone()
        if bad[0]:
            raise SchemaError(f"signals: {bad[0]} filas con p/q fuera de [0,1] o ventana inválida")
    con.close()


def _check_lineage(table: pa.Table, spec: TableSpec) -> None:
    con = duckdb.connect()
    con.register("t", table)
    checks = {
        ELIGIBILITY.name: (
            "group_name NOT IN (SELECT unnest(?)) OR rule = ''",
            [list(ELIGIBILITY_GROUPS)],
            "grupo fuera de dominio o regla vacía",
        ),
        VINTAGE_LINEAGE.name: (
            "status NOT IN (SELECT unnest(?)) OR n_rows < 0 OR (status = 'ok') <> (detail IS NULL)",
            [list(VINTAGE_STATUSES)],
            "status fuera de dominio, n_rows negativo o motivo faltante",
        ),
        DETECTOR_RUNS.name: (
            "n_tests < 0 OR n_bh < 0 OR n_by < 0 OR n_bh > n_tests OR n_by > n_tests "
            "OR q <= 0 OR q >= 1",
            [],
            "conteos o q inconsistentes",
        ),
        SERIES_TERM_LINKS.name: (
            "score < 0 OR score > 1 OR isnan(score)",
            [],
            "score fuera de [0,1]",
        ),
        CLUSTERS.name: ("role NOT IN ('representante', 'miembro')", [], "rol inválido"),
        TEXT_INGEST.name: (
            "status NOT IN (SELECT unnest(?)) OR n_chars < 0",
            [list(TEXT_INGEST_STATUSES)],
            "estado fuera de dominio o n_chars negativo",
        ),
        SIGNALS.name: ("n_tests < 1 OR run_id = ''", [], "n_tests < 1 o run_id vacío"),
    }
    if spec.name in checks:
        where, params, msg = checks[spec.name]
        bad = con.execute(f"SELECT count(*) FROM t WHERE {where}", params).fetchone()[0]
        if bad:
            raise SchemaError(f"{spec.name}: {bad} filas con {msg}")
    con.close()


def _check_unique_key(table: pa.Table, spec: TableSpec) -> None:
    # GROUP BY trata los NULL como iguales: dos filas con vintage_date NULL y misma clave
    # cuentan como duplicado, que es lo que queremos para la idempotencia.
    con = duckdb.connect()
    con.register("t", table)
    cols = ", ".join(spec.key)
    dupes = con.execute(
        f"SELECT count(*) FROM (SELECT {cols} FROM t GROUP BY {cols} HAVING count(*) > 1)"
    ).fetchone()[0]
    con.close()
    if dupes:
        raise SchemaError(f"{spec.name}: {dupes} claves duplicadas en {spec.key}")


def expected_columns(spec: TableSpec) -> list[str]:
    """Nombres de columna del esquema, en orden."""
    return spec.schema.names
