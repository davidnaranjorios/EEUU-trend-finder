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
        # Entidades distintas (agencia, empresa) entre esos documentos; NULL = no medido
        # (particiones anteriores a la versión 2 del esquema de texto, ADR 0008).
        ("n_entities", pa.int64(), True),
        # Industrias distintas (SIC de 2 dígitos, EDGAR) entre esos documentos; NULL = no medido
        # (otras fuentes y particiones anteriores a la versión 4 del esquema de texto).
        ("n_industries", pa.int64(), True),
        # SIC de 4 dígitos (EDGAR) de las empresas que mencionan el término: JSON {sic: documentos},
        # solo los `top_sic` más frecuentes. NULL = no medido (versión del esquema < 5; ADR 0012).
        ("sic_top_json", pa.string(), True),
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

# Una fila por prueba (serie × detector × corrida), haya o no señal: explica por qué una serie
# no dio señal y guarda el p calibrado. q_value/q_by quedan NULL hasta cerrar la familia (fdr).
DETECTOR_TESTS = TableSpec(
    name="detector_tests",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("detector", pa.string(), False),
        ("source", pa.string(), False),
        ("object_id", pa.string(), False),
        ("frequency", pa.string(), False),
        ("grid", pa.string(), False),
        ("n_growth", pa.int64(), False),
        ("block_length", pa.float64(), False),
        ("statistic", pa.float64(), False),
        ("effect_size", pa.float64(), False),
        ("persistent", pa.bool_(), False),
        ("p_value", pa.float64(), False),
        ("p_method", pa.string(), False),  # empirical o gpd; *_esc si se usaron más réplicas
        ("q_value", pa.float64(), True),  # BH
        ("q_by", pa.float64(), True),  # BY, solo comparación
        ("n_tests", pa.int64(), True),
        ("window_start", pa.date32(), False),
        ("window_end", pa.date32(), False),
        ("copyrighted", pa.bool_(), False),
    ),
    key=("run_id", "detector", "source", "object_id"),
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
        # Prueba de co-movimiento (src/rank/comove.py): q de su propia familia BH y rezago.
        # `validated` = q <= fdr.q; solo un vínculo validado cuenta como fuente independiente.
        ("comove_q", pa.float64(), True),
        ("comove_lag", pa.int64(), True),
        ("validated", pa.bool_(), True),
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

# --- Ranking (Fase 5, ADR 0009) -------------------------------------------------------------------

# Un candidato por cluster de series (kind = series) o de términos (kind = text) de la corrida.
# status: candidato (≥ rank.min_sources fuentes independientes), una_fuente o fuera_del_tope.
# published: candidato cuya evidencia pública (sin copyright) también llega al mínimo.
RANK_STATUSES = ("candidato", "una_fuente", "fuera_del_tope")
RANK_CANDIDATES = TableSpec(
    name="rank_candidates",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("candidate_id", pa.string(), False),
        ("kind", pa.string(), False),
        ("status", pa.string(), False),
        ("rank", pa.int64(), True),
        ("score", pa.float64(), True),
        ("n_sources", pa.int64(), False),
        ("n_sources_public", pa.int64(), False),
        ("sources_json", pa.string(), False),  # grupos de agencias independientes
        ("components_json", pa.string(), True),
        ("n_members", pa.int64(), False),
        ("n_terms", pa.int64(), False),
        ("published", pa.bool_(), False),
    ),
    key=("run_id", "candidate_id"),
)

# Evidencia de cada candidato: series con señal (item_kind = series) y términos enlazados
# (item_kind = term). `public` = false para series con copyright (no se publican).
RANK_EVIDENCE = TableSpec(
    name="rank_evidence",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("candidate_id", pa.string(), False),
        ("item_kind", pa.string(), False),
        ("source", pa.string(), False),
        ("object_id", pa.string(), False),
        ("detectors", pa.string(), False),
        ("q_value", pa.float64(), False),
        ("effect_size", pa.float64(), False),
        ("agencies_json", pa.string(), False),
        ("public", pa.bool_(), False),
        ("window_start", pa.date32(), True),
        ("window_end", pa.date32(), True),
        ("link_score", pa.float64(), True),
        ("linked_series", pa.string(), True),
        ("n_entities", pa.int64(), True),
    ),
    key=("run_id", "candidate_id", "item_kind", "source", "object_id"),
)

# Origen del dato de cada serie con evidencia (ADR 0010): identidad usada para triangular, cadena
# de republicación y posibles copias entre agencias ("posible mismo origen", revisión humana).
RANK_ORIGIN = TableSpec(
    name="rank_origin",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("source", pa.string(), False),
        ("object_id", pa.string(), False),
        ("origin_json", pa.string(), False),
        ("chain_json", pa.string(), False),
        ("rule_id", pa.string(), True),
        ("possible_same_origin_json", pa.string(), False),
        ("duplicate_rho", pa.float64(), True),
    ),
    key=("run_id", "source", "object_id"),
)

# Ficha del fenómeno (descriptiva): relacionadas y rezagos por candidato de series.
RANK_CONTEXT = TableSpec(
    name="rank_context",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("candidate_id", pa.string(), False),
        ("payload_json", pa.string(), False),
    ),
    key=("run_id", "candidate_id"),
)

# Anclaje a industria (ADR 0012): una fila por (objeto, NAICS). `peso` suma 1 por objeto y método.
NAICS_METHODS = (
    "codigo_en_metadatos",
    "nombre_oficial",
    "heredado_origen",
    "heredado_cluster",
    "sic_concordancia",
)
NAICS_LINKS = TableSpec(
    name="naics_links",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("source", pa.string(), False),
        ("object_id", pa.string(), False),
        ("naics", pa.string(), False),
        ("naics_name", pa.string(), True),
        ("peso", pa.float64(), False),
        ("metodo", pa.string(), False),
    ),
    key=("run_id", "source", "object_id", "naics", "metodo"),
)

RANK_RUNS = TableSpec(
    name="rank_runs",
    schema=_schema(
        ("run_id", pa.string(), False),
        ("started_at", pa.timestamp("s", tz="UTC"), False),
        ("finished_at", pa.timestamp("s", tz="UTC"), False),
        ("numeric_run_id", pa.string(), True),
        ("text_run_ids_json", pa.string(), False),
        ("n_signal_series", pa.int64(), False),
        ("n_signal_terms", pa.int64(), False),
        ("n_clusters", pa.int64(), False),
        ("n_term_clusters", pa.int64(), False),
        ("n_links", pa.int64(), False),
        ("n_candidates", pa.int64(), False),
        ("n_single_source", pa.int64(), False),
        ("n_published", pa.int64(), False),
        ("params_json", pa.string(), False),
    ),
    key=("run_id",),
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
        DETECTOR_TESTS,
        SERIES_TERM_LINKS,
        CLUSTERS,
        TEXT_INGEST,
        RANK_CANDIDATES,
        RANK_EVIDENCE,
        RANK_ORIGIN,
        RANK_RUNS,
        RANK_CONTEXT,
        NAICS_LINKS,
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
            "OR doc_count > total_docs OR n_entities < 1 OR n_entities > doc_count "
            "OR n_industries < 1 OR n_industries > doc_count"
        ).fetchone()
        if bad[0]:
            raise SchemaError(f"term_freq: {bad[0]} filas con doc_count/total_docs inconsistentes")
    if spec is NAICS_LINKS:
        allowed = ", ".join(f"'{x}'" for x in NAICS_METHODS)
        bad = con.execute(
            f"SELECT count(*) FROM t WHERE metodo NOT IN ({allowed}) OR peso <= 0 OR peso > 1"
        ).fetchone()
        if bad[0]:
            raise SchemaError(f"naics_links: {bad[0]} filas con método o peso fuera de dominio")
    if spec is RANK_CANDIDATES:
        allowed = ", ".join(f"'{x}'" for x in RANK_STATUSES)
        bad = con.execute(
            f"SELECT count(*) FROM t WHERE status NOT IN ({allowed}) "
            "OR (status <> 'una_fuente' AND n_sources < 2) "
            "OR (published AND (status <> 'candidato' OR n_sources_public < 2)) "
            "OR n_sources_public > n_sources"
        ).fetchone()
        if bad[0]:
            raise SchemaError(
                f"rank_candidates: {bad[0]} filas violan la triangulación (≥ 2 fuentes)"
            )
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
        DETECTOR_TESTS.name: (
            "p_value NOT BETWEEN 0 AND 1 OR q_value NOT BETWEEN 0 AND 1 "
            "OR q_by NOT BETWEEN 0 AND 1 "
            "OR p_method NOT IN ('empirical', 'gpd', 'empirical_esc', 'gpd_esc') "
            "OR window_start > window_end OR n_tests < 1",
            [],
            "p/q fuera de [0,1], método o ventana inválidos",
        ),
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
