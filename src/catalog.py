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

from src.schemas import ELIGIBILITY, SERIES, SERIES_META, TableSpec

# Vista -> (subdirectorio bajo processed/, esquema). Cada fuente escribe <dir>/<fuente>/<NN>/.
VIEWS: dict[str, tuple[str, TableSpec]] = {
    "series": ("series", SERIES),
    "series_meta": ("series_meta", SERIES_META),
    "eligibility": ("lineage/eligibility", ELIGIBILITY),
}
# Credenciales de R2 en .env (nombres estándar de S3, que también leen rclone y DuckDB).
# Nunca van en config.yaml.
R2_ENV = ("CLOUDFLARE_ACCOUNT_ID", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def connect(processed_dir: Path | str, database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    """Conexión con una vista por tabla; vacía (con su esquema) si aún no hay datos."""
    con = duckdb.connect(database)
    for view, (subdir, spec) in VIEWS.items():
        files = sorted((Path(processed_dir) / subdir).glob("*/??/*.parquet"))
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
    """Conteos de verificación: filas, vigentes vs. fechadas y embudo de la última corrida."""
    rows, n_series, current, dated, n_dated = con.execute(
        "SELECT count(*), count(DISTINCT series_id), count(*) FILTER (vintage_date IS NULL), "
        "count(*) FILTER (vintage_date IS NOT NULL), "
        "count(DISTINCT series_id) FILTER (vintage_date IS NOT NULL) FROM series"
    ).fetchone()
    groups = con.execute(
        "SELECT group_name, count(*) FROM eligibility "
        "WHERE run_id = (SELECT max_by(run_id, decided_at) FROM eligibility) "
        "GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    run_id = con.execute("SELECT max_by(run_id, decided_at) FROM eligibility").fetchone()[0]
    return {
        "series": {
            "rows": rows,
            "series": n_series,
            "current_rows": current,
            "vintage_rows": dated,
            "series_with_vintages": n_dated,
        },
        "series_meta": con.execute("SELECT count(*) FROM series_meta").fetchone()[0],
        "eligibility": {"run_id": run_id, "groups": dict(groups)},
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
