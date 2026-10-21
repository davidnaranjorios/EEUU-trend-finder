"""Vistas DuckDB sobre los Parquet de data/processed."""

from __future__ import annotations

from pathlib import Path

import duckdb

from src.schemas import SERIES, SERIES_META, TableSpec

# Tabla -> subdirectorio bajo processed/. Cada fuente escribe <tabla>/<fuente>/<NN>/*.parquet.
VIEWS: dict[str, tuple[str, TableSpec]] = {
    "series": ("series", SERIES),
    "series_meta": ("series_meta", SERIES_META),
}


def connect(processed_dir: Path | str, database: str = ":memory:") -> duckdb.DuckDBPyConnection:
    """Conexión con una vista por tabla común; vacía (con su esquema) si aún no hay datos."""
    con = duckdb.connect(database)
    for view, (subdir, spec) in VIEWS.items():
        files = sorted((Path(processed_dir) / subdir).glob("*/??/*.parquet"))
        if files:
            listing = ", ".join("'" + str(f).replace("'", "''") + "'" for f in files)
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet([{listing}])")
        else:
            con.register(f"_{view}_empty", spec.schema.empty_table())
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM _{view}_empty")
    return con
