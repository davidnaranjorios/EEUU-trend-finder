"""`uv run python -m src.market build`: métricas de mercado por NAICS y año (ADR 0024).

Lee las series de `cbp`, `bds` y `econcensus` (esquema común) y escribe
`data/processed/market/naics_metrics/part-0.parquet`, clave `(naics, metric, year)`. Con la fuente
`bea_io` escribe además `market/io_flows` (ADR 0032). Se reescribe completa en cada corrida
(idempotente). Una métrica = una fuente; cuando dos fuentes miden lo
mismo con otra definición, la métrica de la segunda lleva su origen en el nombre (`*_bds`).
Estas métricas alimentan las fichas; no pasan por los detectores ni por el ranking.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.config import load_config
from src.ingest.common import resolve
from src.ingest.store import read_table
from src.log import get_logger
from src.schemas import SERIES, SERIES_META, TableSpec, validate

MARKET = TableSpec(
    name="naics_metrics",
    schema=pa.schema(
        [
            pa.field("naics", pa.string(), nullable=False),
            pa.field("metric", pa.string(), nullable=False),
            pa.field("year", pa.int32(), nullable=False),
            pa.field("value", pa.float64(), nullable=False),
            pa.field("units", pa.string()),
            pa.field("source", pa.string(), nullable=False),
            pa.field("series_id", pa.string()),
        ]
    ),
    key=("naics", "metric", "year"),
)

IO_FLOWS = TableSpec(
    name="io_flows",
    schema=pa.schema(
        [
            pa.field("year", pa.int32(), nullable=False),
            pa.field("from_industry", pa.string(), nullable=False),  # código BEA de la fila
            pa.field("to_industry", pa.string(), nullable=False),  # código BEA de la columna
            pa.field("from_desc", pa.string()),
            pa.field("to_desc", pa.string()),
            pa.field("from_kind", pa.string(), nullable=False),  # industria | total | otro
            # industria | demanda_final | total | otro
            pa.field("to_kind", pa.string(), nullable=False),
            pa.field("demand_category", pa.string()),  # categoría de la demanda final, si aplica
            pa.field("from_naics", pa.string()),  # NAICS 2022 del código BEA, separados por «;»
            pa.field("to_naics", pa.string()),
            pa.field("value", pa.float64(), nullable=False),
            pa.field("units", pa.string()),
            pa.field("source", pa.string(), nullable=False),
            pa.field("series_id", pa.string()),
        ]
    ),
    key=("year", "from_industry", "to_industry"),
)
IO_SERIES_RE = re.compile(
    r"^InputOutput:(?P<table>[^:]+):(?P<row>[^.:]+)\.[^.:]*\.(?P<col>[^.:]+)\.[^.:]*:A$"
)
IO_TITLE_TAIL_RE = re.compile(r" \[[^\]]*\]$")

ID_RE = re.compile(
    r"^(?P<src>cbp|bds|econcensus):(?P<var>[A-Za-z0-9_]+):(?P<naics>[^:]+)(?::(?P<cls>.+))?$"
)
CR_RE = re.compile(r"^CR(\d+)$")
LESS_RE = re.compile(r"less than (\d+) employees", re.IGNORECASE)
RANGE_RE = re.compile(r"(\d+) to (\d+) employees", re.IGNORECASE)
OPEN_RE = re.compile(r"(\d+) employees or more", re.IGNORECASE)


def size_upper(title: str) -> float | None:
    """Tope de empleados de la clase de tamaño (inf = sin tope); None si no es clase por empleo."""
    if m := LESS_RE.search(title):
        return int(m.group(1)) - 1
    if m := RANGE_RE.search(title):
        return int(m.group(2))
    if OPEN_RE.search(title):
        return float("inf")
    return None


def build_rows(
    series: pa.Table,
    meta: pa.Table,
    *,
    small_max_employees: int,
    metrics: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Filas `naics_metrics` a partir de las series de las tres fuentes y sus títulos."""
    titles = {(r["source"], r["series_id"]): r["title"] or "" for r in meta.to_pylist()}
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    sizes: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)

    def put(
        naics: str, metric: str, year: int, value: float, units: str | None, src: str, sid: str
    ) -> None:
        out[(naics, metric, year)] = {
            "naics": naics,
            "metric": metric,
            "year": year,
            "value": float(value),
            "units": units,
            "source": src,
            "series_id": sid,
        }

    for r in series.to_pylist():
        m = ID_RE.match(r["series_id"])
        if m is None or r["value"] is None:
            continue
        src, var, naics, cls = m["src"], m["var"], m["naics"], m["cls"]
        year = r["date"].year
        if cr := CR_RE.match(var):
            put(naics, f"cr{cr.group(1)}", year, r["value"], r["units"], src, r["series_id"])
        elif var == "FIRM_SIZE":
            upper = size_upper(titles.get((src, r["series_id"]), ""))
            if upper is not None:
                sizes[(naics, year)].append((upper, r["value"]))
        elif var in metrics.get(src, {}) and cls is None:
            put(naics, metrics[src][var], year, r["value"], r["units"], src, r["series_id"])
    for (naics, year), classes in sizes.items():
        total = sum(v for _, v in classes)
        if total > 0:
            small = sum(v for ub, v in classes if ub < small_max_employees)
            put(naics, "small_firm_share", year, 100.0 * small / total, "Percent", "econcensus", "")
    return sorted(out.values(), key=lambda r: (r["naics"], r["metric"], r["year"]))


def _io_descriptions(title: str) -> tuple[str, str]:
    """(descripción de la fila, de la columna) del título «fila → columna — tabla [clave]»."""
    base = IO_TITLE_TAIL_RE.sub("", title or "").rsplit(" — ", 1)[0]
    row, _, col = base.partition(" → ")
    return row.strip(), col.strip()


def build_io_flows(
    series: pa.Table,
    meta: pa.Table,
    *,
    bea_naics: dict[str, list[str]],
    demand: dict[str, str],
    total_regex: str,
    units: str | None,
) -> list[dict[str, Any]]:
    """Filas `io_flows` de la tabla de uso (fila = producto que se vende, columna = quien lo usa).

    - Una columna es **industria** si su código también es una fila (el mismo código de BEA sirve
      de producto y de industria); si no, es **total** cuando su descripción empieza con «Total»
      y **demanda final** en el resto, con la categoría que dé la primera expresión de `demand`
      que coincida con la descripción oficial (gobierno antes que inversión).
    - Una fila es **industria** si su código es columna industria; **total** si su descripción
      empieza con «Total»; **otro** (valor agregado, impuestos...) si no.
    - Los NAICS salen de la concordancia oficial de BEA; sin código en ella, nulo («sin dato»).
    """
    titles = {(r["source"], r["series_id"]): r["title"] or "" for r in meta.to_pylist()}
    cells: list[tuple[int, str, str, float, str, str, str]] = []
    row_codes: set[str] = set()
    for r in series.to_pylist():
        m = IO_SERIES_RE.match(r["series_id"])
        if m is None or r["value"] is None:
            continue
        rd, cd = _io_descriptions(titles.get((r["source"], r["series_id"]), ""))
        cells.append(
            (r["date"].year, m["row"], m["col"], float(r["value"]), rd, cd, r["series_id"])
        )
        row_codes.add(m["row"])
    total_re = re.compile(total_regex)
    patterns = [(name, re.compile(rx)) for name, rx in demand.items()]
    to_kind: dict[str, tuple[str, str | None]] = {}
    for _, _, col, _, _, cd, _ in cells:
        if col in to_kind:
            continue
        if col in row_codes:
            to_kind[col] = ("industria", None)
        elif total_re.search(cd):
            to_kind[col] = ("total", None)
        else:
            cat = next((n for n, rx in patterns if rx.search(cd)), None)
            to_kind[col] = ("demanda_final", cat)
    ind_cols = {c for c, (k, _) in to_kind.items() if k == "industria"}

    def naics(code: str) -> str | None:
        return ";".join(sorted(set(bea_naics.get(code, [])))) or None

    out: dict[tuple[int, str, str], dict[str, Any]] = {}
    for year, row, col, value, rd, cd, sid in cells:
        kind, cat = to_kind[col]
        fk = "industria" if row in ind_cols else "total" if total_re.search(rd) else "otro"
        out[(year, row, col)] = {
            "year": year,
            "from_industry": row,
            "to_industry": col,
            "from_desc": rd or None,
            "to_desc": cd or None,
            "from_kind": fk,
            "to_kind": kind,
            "demand_category": cat,
            "from_naics": naics(row) if fk == "industria" else None,
            "to_naics": naics(col) if kind == "industria" else None,
            "value": value,
            "units": units,
            "source": "bea_io",
            "series_id": sid,
        }
    return sorted(out.values(), key=lambda r: (r["year"], r["from_industry"], r["to_industry"]))


def _write_part(table: pa.Table, spec: TableSpec, dest: Path) -> None:
    validate(table, spec)
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / ".part-0.parquet.tmp"
    pq.write_table(table, tmp)
    tmp.replace(dest / "part-0.parquet")


def io_flows(cfg: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Escribe `market/io_flows` si la fuente `bea_io` tiene series; None si no hay datos."""
    series = read_table(SERIES, root / "series", source="bea_io")
    if series.num_rows == 0:
        return None
    meta = read_table(SERIES_META, root / "series_meta", source="bea_io")
    from src.config import REPO_ROOT
    from src.rank import naics as naics_mod

    tables = naics_mod.load_tables(cfg, REPO_ROOT)
    mc = cfg["market"]["io"]
    rows = build_io_flows(
        series,
        meta,
        bea_naics={k: [c for c, _ in v] for k, v in tables.bea.items()},
        demand=mc["demand_categories"],
        total_regex=mc["total_regex"],
        units=mc.get("units"),
    )
    table = pa.Table.from_pylist(rows, schema=IO_FLOWS.schema)
    _write_part(table, IO_FLOWS, root / "market" / "io_flows")
    return {
        "rows": len(rows),
        "years": [min(r["year"] for r in rows), max(r["year"] for r in rows)] if rows else None,
        "demand_categories": sorted({r["demand_category"] for r in rows if r["demand_category"]}),
        "columns_without_category": sorted(
            {
                r["to_industry"]
                for r in rows
                if r["to_kind"] == "demanda_final" and not r["demand_category"]
            }
        ),
    }


def build(cfg: dict[str, Any], processed: Path | None = None) -> dict[str, Any]:
    root = processed or resolve(cfg["paths"]["processed_dir"])
    parts, metas = [], []
    for src in ("cbp", "bds", "econcensus"):
        parts.append(read_table(SERIES, root / "series", source=src))
        metas.append(read_table(SERIES_META, root / "series_meta", source=src))
    series, meta = pa.concat_tables(parts), pa.concat_tables(metas)
    rows = build_rows(
        series,
        meta,
        small_max_employees=int(cfg["market"]["small_firm_max_employees"]),
        metrics=cfg["market"]["metrics"],
    )
    table = pa.Table.from_pylist(rows, schema=MARKET.schema)
    validate(table, MARKET)
    _write_part(table, MARKET, root / "market" / "naics_metrics")
    metrics = sorted({r["metric"] for r in rows})
    out = {"rows": len(rows), "naics": len({r["naics"] for r in rows}), "metrics": metrics}
    if (io := io_flows(cfg, root)) is not None:
        out["io_flows"] = io
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.market")
    parser.add_argument("cmd", choices=["build"])
    parser.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    cfg = load_config()
    log = get_logger("market", cfg["logging"]["level"])
    stats = build(cfg)
    log.info("market build", extra={"fields": stats})
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
