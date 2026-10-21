"""Esquemas comunes (ver CLAUDE.md, "Esquemas comunes") y su validación."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pyarrow as pa

from src.config import FREQUENCIES


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

TERM_FREQ = TableSpec(
    name="term_freq",
    schema=_schema(
        ("source", pa.string(), False),
        ("period", pa.string(), False),
        ("term", pa.string(), False),
        ("doc_count", pa.int64(), False),
        ("total_docs", pa.int64(), False),
    ),
    key=("source", "period", "term"),
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
    ),
    key=("signal_id",),
)

ALL_SPECS = {spec.name: spec for spec in (SERIES, SERIES_META, TEXT_DOCS, TERM_FREQ, SIGNALS)}


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
