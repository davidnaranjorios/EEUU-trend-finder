"""Vistas DuckDB sobre los Parquet de data/processed, en disco local o en Cloudflare R2."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb

from src.schemas import (
    CLUSTERS,
    DETECTOR_RUNS,
    DETECTOR_TESTS,
    ELIGIBILITY,
    RANK_CANDIDATES,
    RANK_CONTEXT,
    RANK_EVIDENCE,
    RANK_RUNS,
    SERIES,
    SERIES_META,
    SERIES_TERM_LINKS,
    SIGNALS,
    TERM_FREQ,
    TEXT_DOCS,
    TEXT_INGEST,
    VINTAGE_LINEAGE,
    TableSpec,
)

# Vista -> (subdirectorio bajo processed/, esquema). Cada fuente escribe <dir>/<fuente>/<NN>/.
VIEWS: dict[str, tuple[str, TableSpec]] = {
    "series": ("series", SERIES),
    "series_meta": ("series_meta", SERIES_META),
    "eligibility": ("lineage/eligibility", ELIGIBILITY),
    "vintage_lineage": ("lineage/vintages", VINTAGE_LINEAGE),
    # Texto (ADR 0005): particiones por periodo, <dir>/<fuente>/<periodo>/.
    "text_docs": ("text_docs", TEXT_DOCS),
    "term_freq": ("term_freq", TERM_FREQ),
    "text_ingest": ("lineage/text_ingest", TEXT_INGEST),
}
# Salidas de los detectores (Fase 2). Solo en la conexión local: en Actions se consultan sobre la
# copia traída de R2, y sobre R2 una vista sin archivos fallaría antes de la primera corrida.
# Rutas relativas a data/ (signals vive en data/signals, fuera de processed).
DETECT_VIEWS: dict[str, tuple[str, TableSpec]] = {
    "detector_runs": ("processed/lineage/detector_runs", DETECTOR_RUNS),
    "detector_tests": ("processed/lineage/detector_tests", DETECTOR_TESTS),
    "signals": ("signals", SIGNALS),
    # Ranking (Fase 5, ADR 0009): partición única "rank".
    "clusters": ("processed/rank/clusters", CLUSTERS),
    "series_term_links": ("processed/rank/series_term_links", SERIES_TERM_LINKS),
    "rank_candidates": ("processed/rank/rank_candidates", RANK_CANDIDATES),
    "rank_evidence": ("processed/rank/rank_evidence", RANK_EVIDENCE),
    "rank_runs": ("processed/rank/rank_runs", RANK_RUNS),
    "rank_context": ("processed/rank/rank_context", RANK_CONTEXT),
}
# Nombre del subdirectorio de partición: bucket de dos dígitos (series) o periodo (texto).
PARTITION_GLOB = {"text_docs": "*", "term_freq": "*", "text_ingest": "*"}
# Credenciales de R2 en .env (nombres estándar de S3, que también leen rclone y DuckDB).
# Nunca van en config.yaml.
R2_ENV = ("CLOUDFLARE_ACCOUNT_ID", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def connect(processed_dir: Path | str, database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    """Conexión con una vista por tabla; vacía (con su esquema) si aún no hay datos."""
    con = duckdb.connect(database)
    data_dir = Path(processed_dir).parent
    views = {view: (Path(processed_dir) / sub, spec) for view, (sub, spec) in VIEWS.items()}
    views |= {view: (data_dir / sub, spec) for view, (sub, spec) in DETECT_VIEWS.items()}
    for view, (root, spec) in views.items():
        part = PARTITION_GLOB.get(view, "??")
        files = sorted(root.glob(f"*/{part}/*.parquet"))
        if files:
            listing = ", ".join(_quote(str(f)) for f in files)
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet([{listing}])")
        else:
            con.register(f"_{view}_empty", spec.schema.empty_table())
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM _{view}_empty")
    return con


def connect_r2(
    storage: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    database: str = ":memory:",
) -> duckdb.DuckDBPyConnection:
    """Las mismas vistas, leídas directamente de R2 con httpfs (ADR 0002)."""
    con = duckdb.connect(database)
    for stmt in r2_statements(storage, os.environ if env is None else env):
        con.execute(stmt)
    return con


def r2_statements(storage: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    """SQL que crea el secreto de R2 y una vista por tabla sobre `r2://bucket/prefix/...`."""
    missing = [k for k in R2_ENV if not env.get(k)]
    if missing:
        raise RuntimeError(f"faltan variables de R2 en el entorno: {', '.join(missing)}")
    base = f"r2://{storage['bucket']}/{storage['prefix'].strip('/')}"
    stmts = [
        "INSTALL httpfs",
        "LOAD httpfs",
        "CREATE OR REPLACE SECRET r2 (TYPE r2, "
        f"KEY_ID {_quote(env['AWS_ACCESS_KEY_ID'])}, "
        f"SECRET {_quote(env['AWS_SECRET_ACCESS_KEY'])}, "
        f"ACCOUNT_ID {_quote(account_id(env['CLOUDFLARE_ACCOUNT_ID']))})",
    ]
    for view, (subdir, _) in VIEWS.items():
        if view in PARTITION_GLOB:
            # Las tablas de texto pueden no existir aún en R2 y DuckDB falla al crear una vista
            # sobre un glob vacío; se consultan sobre la copia local (infra/pull_r2.sh).
            continue
        glob = _quote(f"{base}/{subdir}/*/*/*.parquet")
        stmts.append(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet({glob})")
    return stmts


def account_id(value: str) -> str:
    """Account ID de Cloudflare aunque llegue con espacios o dentro de la URL del endpoint.

    Misma limpieza que infra/r2_env.sh.
    """
    v = "".join(value.split()).removeprefix("https://").removeprefix("http://")
    v = v.split("/", 1)[0].split(".", 1)[0].lower()
    if not re.fullmatch(r"[0-9a-f]{32}", v):
        raise RuntimeError(
            "CLOUDFLARE_ACCOUNT_ID no es un Account ID de Cloudflare: se esperan 32 caracteres "
            f"hexadecimales y, tras limpiar, quedan {len(v)}"
        )
    return v


def summary(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Conteos de verificación: filas, vigentes vs. fechadas, por fuente y embudo por fuente."""
    rows, n_series, current, dated, n_dated = con.execute(
        "SELECT count(*), count(DISTINCT series_id), count(*) FILTER (vintage_date IS NULL), "
        "count(*) FILTER (vintage_date IS NOT NULL), "
        "count(DISTINCT series_id) FILTER (vintage_date IS NOT NULL) FROM series"
    ).fetchone()
    # Embudo por fuente: la última decisión de cada serie (las fuentes de la Fase 4 solo
    # reprocesan las unidades que cambian, ADR 0007). Una fila agregada `<unidad>/*` cuenta por
    # las n_series que resume.
    funnel_rows = con.execute(
        "SELECT source, group_name, sum(CASE WHEN ends_with(series_id, '/*') THEN coalesce("
        "TRY_CAST(regexp_extract(detail, 'n_series=([0-9]+)', 1) AS BIGINT), 1) ELSE 1 END), "
        "count(*) FROM (SELECT * FROM eligibility QUALIFY row_number() OVER (PARTITION BY "
        "source, series_id ORDER BY decided_at DESC, run_id DESC) = 1) "
        "GROUP BY 1, 2 ORDER BY 1, 3 DESC"
    ).fetchall()
    last_runs = dict(
        con.execute(
            "SELECT source, max_by(run_id, decided_at) FROM eligibility GROUP BY 1"
        ).fetchall()
    )
    eligibility: dict[str, Any] = {}
    for source, group, n, lineage_rows in funnel_rows:
        e = eligibility.setdefault(
            source, {"last_run_id": last_runs[source], "groups": {}, "lineage_rows": 0}
        )
        e["groups"][group] = int(n)
        e["lineage_rows"] += int(lineage_rows)
    per_source = con.execute(
        "SELECT source, count(*), count(DISTINCT series_id) FROM series GROUP BY 1 ORDER BY 1"
    ).fetchall()
    meta_by_source = dict(
        con.execute("SELECT source, count(*) FROM series_meta GROUP BY 1").fetchall()
    )
    # Motivo vigente de cada serie de la muestra: el de su último intento.
    vintage_status = con.execute(
        "SELECT status, count(*), list(series_id ORDER BY series_id) FILTER (status <> 'ok') "
        "FROM (SELECT series_id, max_by(status, decided_at) AS status FROM vintage_lineage "
        "GROUP BY series_id) GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return {
        "series": {
            "rows": rows,
            "series": n_series,
            "current_rows": current,
            "vintage_rows": dated,
            "series_with_vintages": n_dated,
        },
        "series_meta": con.execute("SELECT count(*) FROM series_meta").fetchone()[0],
        "by_source": {
            source: {"rows": rows_s, "series": n_s, "series_meta": meta_by_source.get(source, 0)}
            for source, rows_s, n_s in per_source
        },
        "eligibility": eligibility,
        "vintage_lineage": {
            status: {"series": n, **({"ids": ids} if status != "ok" else {})}
            for status, n, ids in vintage_status
        },
    }


def main(argv: list[str] | None = None) -> None:
    """`python -m src.catalog summary [--r2]`: conteos de verificación en JSON."""
    from src.config import load_config

    parser = argparse.ArgumentParser(prog="python -m src.catalog")
    parser.add_argument("command", choices=["summary"])
    parser.add_argument("--r2", action="store_true", help="leer de R2 en vez de data/processed")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.r2:
        from dotenv import load_dotenv

        load_dotenv()
        con = connect_r2(cfg["storage"])
    else:
        con = connect(cfg["paths"]["processed_dir"])
    print(json.dumps(summary(con), indent=2, ensure_ascii=False))


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


if __name__ == "__main__":
    main()
