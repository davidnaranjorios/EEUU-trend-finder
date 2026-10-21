"""Almacén Parquet idempotente particionado por fuente y bucket.

Estructura: `<root>/<source>/<NN>/part-<uuid>.parquet`, con `NN = sha1(clave) % n_buckets`.
Volver a escribir filas idénticas no añade nada. Si una clave llega con valores distintos, se
reescribe el bucket conservando la versión nueva y se informa el conteo.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from src.schemas import TableSpec, validate

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
