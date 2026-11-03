"""Almacén Parquet idempotente particionado por fuente y bucket.

Estructura: `<root>/<source>/<NN>/part-<uuid>.parquet`, con `NN = sha1(clave) % n_buckets`.
Volver a escribir filas idénticas no añade nada. Si una clave llega con valores distintos, se
reescribe el bucket conservando la versión nueva y se informa el conteo.

Cada `upsert` con filas nuevas añade un `part-*.parquet` por bucket tocado. `compact` los junta
en uno por bucket (ADR 0004): sin ella, la descarga inicial dejó 14.491 archivos en R2 y leerlos
por httpfs no terminaba en 30 min. Uso: `uv run python -m src.ingest.store compact [DIR]`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from src.schemas import TableSpec, validate

BUCKET_DIR = re.compile(r"^\d{2}$")
SWAP_DIR = re.compile(r"^\d{2}(\.(old|new)-.+)?$")
SORT_COLUMNS = ("source", "series_id", "run_id", "date", "vintage_date")

MAX_BUCKETS = 100  # los buckets son directorios de dos dígitos


@dataclass
class UpsertResult:
    inserted: int = 0
    unchanged: int = 0
    replaced: int = 0

    def __iadd__(self, other: UpsertResult) -> UpsertResult:
        self.inserted += other.inserted
        self.unchanged += other.unchanged
        self.replaced += other.replaced
        return self


def bucket_of(value: str, n_buckets: int) -> int:
    """Bucket estable (no depende de la semilla de hash de Python)."""
    return int(hashlib.sha1(value.encode()).hexdigest(), 16) % n_buckets


def upsert(
    table: pa.Table,
    spec: TableSpec,
    root: Path,
    *,
    source: str,
    n_buckets: int,
    bucket_col: str = "series_id",
) -> UpsertResult:
    """Inserta las filas nuevas de `table`; ignora las idénticas; reemplaza las que cambian."""
    if not 0 < n_buckets <= MAX_BUCKETS:
        raise ValueError(f"n_buckets debe estar en 1..{MAX_BUCKETS}")
    validate(table, spec)
    result = UpsertResult()
    if table.num_rows == 0:
        return result

    source_dir = Path(root) / source
    recover(source_dir)
    buckets = pa.array(
        [bucket_of(v, n_buckets) for v in table.column(bucket_col).to_pylist()], pa.int32()
    )
    con = duckdb.connect()
    try:
        for b in sorted(set(buckets.to_pylist())):
            sub = table.filter(pc.equal(buckets, b))
            result += _upsert_bucket(con, sub, spec, source_dir / f"{b:02d}", bucket_col)
    finally:
        con.close()
    return result


def _upsert_bucket(
    con: duckdb.DuckDBPyConnection,
    new: pa.Table,
    spec: TableSpec,
    bucket_dir: Path,
    bucket_col: str,
) -> UpsertResult:
    files = _parquet_files(bucket_dir)
    if not files:
        _write_part(bucket_dir, new)
        return UpsertResult(inserted=new.num_rows)

    cols = spec.schema.names
    con.register("new_rows", new)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE old_rows AS SELECT {', '.join(cols)} FROM read_parquet(?) "
        f"WHERE {bucket_col} IN (SELECT DISTINCT {bucket_col} FROM new_rows)",
        [[str(f) for f in files]],
    )
    same_key = " AND ".join(f"n.{k} IS NOT DISTINCT FROM o.{k}" for k in spec.key)
    same_row = " AND ".join(f"n.{c} IS NOT DISTINCT FROM o.{c}" for c in cols)
    unchanged, replaced, inserted = con.execute(
        f"""
        SELECT
          count(*) FILTER (WHERE EXISTS (SELECT 1 FROM old_rows o WHERE {same_row})),
          count(*) FILTER (WHERE EXISTS (SELECT 1 FROM old_rows o WHERE {same_key})
                           AND NOT EXISTS (SELECT 1 FROM old_rows o WHERE {same_row})),
          count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM old_rows o WHERE {same_key}))
        FROM new_rows n
        """
    ).fetchone()

    if replaced:
        # Reescribe el bucket completo: filas viejas sin clave en el lote + lote nuevo.
        kept = con.execute(
            f"SELECT {', '.join(cols)} FROM read_parquet(?) o "
            f"WHERE NOT EXISTS (SELECT 1 FROM new_rows n WHERE {same_key}) "
            f"UNION ALL SELECT {', '.join(cols)} FROM new_rows",
            [[str(f) for f in files]],
        ).to_arrow_table()
        _swap_bucket(bucket_dir, _conform(kept, spec))
    elif inserted:
        rows = con.execute(
            f"SELECT {', '.join(cols)} FROM new_rows n "
            f"WHERE NOT EXISTS (SELECT 1 FROM old_rows o WHERE {same_key})"
        ).to_arrow_table()
        _write_part(bucket_dir, _conform(rows, spec))
    con.unregister("new_rows")
    return UpsertResult(inserted=inserted, unchanged=unchanged, replaced=replaced)


def _conform(table: pa.Table, spec: TableSpec) -> pa.Table:
    out = table.select(spec.schema.names).cast(spec.schema)
    validate(out, spec)
    return out


def _parquet_files(bucket_dir: Path) -> list[Path]:
    return sorted(bucket_dir.glob("*.parquet")) if bucket_dir.is_dir() else []


def _write_part(bucket_dir: Path, table: pa.Table) -> None:
    bucket_dir.mkdir(parents=True, exist_ok=True)
    name = f"part-{uuid.uuid4().hex}"
    tmp = bucket_dir / f".{name}.tmp"
    pq.write_table(table, tmp)
    os.replace(tmp, bucket_dir / f"{name}.parquet")


def _swap_bucket(bucket_dir: Path, table: pa.Table) -> None:
    token = uuid.uuid4().hex
    new_dir = bucket_dir.with_name(f"{bucket_dir.name}.new-{token}")
    old_dir = bucket_dir.with_name(f"{bucket_dir.name}.old-{token}")
    _write_part(new_dir, table)
    os.rename(bucket_dir, old_dir)
    os.rename(new_dir, bucket_dir)
    shutil.rmtree(old_dir)


def recover(source_dir: Path) -> None:
    """Deshace un swap interrumpido: restaura `NN.old-*` si falta `NN` y limpia restos."""
    if not source_dir.is_dir():
        return
    for path in sorted(source_dir.iterdir()):
        name = path.name
        if ".old-" in name:
            target = source_dir / name.split(".old-")[0]
            if target.exists():
                shutil.rmtree(path)
            else:
                os.rename(path, target)
    for path in source_dir.glob("*.new-*"):
        shutil.rmtree(path)


@dataclass
class CompactResult:
    buckets_compacted: int = 0
    files_before: int = 0
    files_after: int = 0
    rows: int = 0


def compact(root: Path) -> CompactResult:
    """Junta en un solo archivo cada bucket `<tabla>/<fuente>/<NN>/` bajo `root` con más de uno.

    Usa el mismo swap atómico que `upsert` (recuperable con `recover`) y verifica que el número
    de filas no cambie. Los buckets que ya tienen un archivo no se tocan, así el sync a R2 solo
    sube lo que cambió. Las filas se ordenan para que el resultado sea determinista.
    """
    result = CompactResult()
    root = Path(root)
    if not root.is_dir():
        return result
    # Primero deshace swaps cortados (`NN.old-*`/`NN.new-*`), aunque falte el `NN` original.
    for source_dir in sorted({p.parent for p in root.rglob("*") if SWAP_DIR.match(p.name)}):
        recover(source_dir)
    for bucket_dir in sorted(p for p in root.rglob("*") if _is_bucket_dir(p)):
        files = _parquet_files(bucket_dir)
        result.files_before += len(files)
        if len(files) <= 1:
            result.files_after += len(files)
            continue
        table = pa.concat_tables([pq.read_table(f) for f in files])
        sort_cols = [c for c in SORT_COLUMNS if c in table.column_names]
        if sort_cols:
            table = table.sort_by([(c, "ascending") for c in sort_cols])
        _swap_bucket(bucket_dir, table)
        after = _parquet_files(bucket_dir)
        n_after = sum(pq.ParquetFile(f).metadata.num_rows for f in after)
        if n_after != table.num_rows:
            raise RuntimeError(f"compact: {bucket_dir}: {n_after} filas de {table.num_rows}")
        result.buckets_compacted += 1
        result.files_after += len(after)
        result.rows += table.num_rows
    return result


def count_files(root: Path) -> int:
    """Archivos Parquet bajo `root` (lo que cuenta `rclone size` en R2)."""
    return sum(1 for _ in Path(root).rglob("*.parquet")) if Path(root).is_dir() else 0


def _is_bucket_dir(path: Path) -> bool:
    return path.is_dir() and bool(BUCKET_DIR.match(path.name))


def read_table(
    spec: TableSpec,
    root: Path,
    *,
    source: str,
    where: str | None = None,
    params: list | None = None,
) -> pa.Table:
    """Lee todas las particiones de `source` (opcionalmente filtradas con SQL `where`)."""
    source_dir = Path(root) / source
    recover(source_dir)
    files = sorted(source_dir.glob("??/*.parquet")) if source_dir.is_dir() else []
    if not files:
        return spec.schema.empty_table()
    con = duckdb.connect()
    try:
        sql = f"SELECT {', '.join(spec.schema.names)} FROM read_parquet(?)"
        if where:
            sql += f" WHERE {where}"
        table = con.execute(sql, [[str(f) for f in files], *(params or [])]).to_arrow_table()
    finally:
        con.close()
    return table.cast(spec.schema)


# --- Particiones por periodo (texto, ADR 0005) ------------------------------------------------


def replace_partition(
    table: pa.Table, spec: TableSpec, root: Path, *, source: str, partition: str
) -> int:
    """Reemplaza `<root>/<source>/<partition>/` completa por `table` (un archivo); idempotente.

    Las tablas de texto se escriben por periodo: reprocesar un periodo lo reescribe entero, así
    que volver a correr no duplica filas ni archivos. Devuelve el número de filas escritas.
    """
    validate(table, spec)
    source_dir = Path(root) / source
    recover(source_dir)
    part_dir = source_dir / partition
    if part_dir.exists():
        _swap_bucket(part_dir, _conform(table, spec))
    else:
        _write_part(part_dir, _conform(table, spec))
    return table.num_rows


def read_partitions(spec: TableSpec, root: Path, *, source: str) -> pa.Table:
    """Todas las particiones por periodo de `source` (vacía con su esquema si no hay)."""
    source_dir = Path(root) / source
    recover(source_dir)
    files = sorted(source_dir.glob("*/*.parquet")) if source_dir.is_dir() else []
    if not files:
        return spec.schema.empty_table()
    tables = []
    for f in files:
        t = pq.read_table(f)
        for field in spec.schema:  # columnas nullable agregadas después (p. ej. n_entities)
            if field.name not in t.column_names and field.nullable:
                t = t.append_column(field.name, pa.nulls(t.num_rows, field.type))
        tables.append(t.select(spec.schema.names))
    return pa.concat_tables(tables).cast(spec.schema)


def main(argv: list[str] | None = None) -> int:
    """`python -m src.ingest.store compact [DIR]`: compacta data/processed (o DIR)."""
    from src.config import REPO_ROOT, load_config
    from src.log import run

    parser = argparse.ArgumentParser(prog="python -m src.ingest.store")
    parser.add_argument("command", choices=["compact"])
    parser.add_argument("root", nargs="?", type=Path, default=None)
    args = parser.parse_args(argv)
    cfg = load_config()
    root = args.root or REPO_ROOT / cfg["paths"]["processed_dir"]
    runs_file = Path(cfg["logging"]["runs_file"])
    runs_file = runs_file if runs_file.is_absolute() else REPO_ROOT / runs_file
    sources = sorted({p.parent.name for p in Path(root).rglob("*") if _is_bucket_dir(p)})
    with run(sources or ["ninguna"], runs_file) as record:
        with record.stage("compact"):
            res = compact(root)
        record.details = {"command": "compact", "stats": res.__dict__}
    print(json.dumps(res.__dict__))
    return 0


if __name__ == "__main__":
    sys.exit(main())
