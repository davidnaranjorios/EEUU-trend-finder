"""`uv run python -m src.market build`: métricas de mercado por NAICS y año (ADR 0024).

Lee las series de `cbp`, `bds` y `econcensus` (esquema común) y escribe
`data/processed/market/naics_metrics/part-0.parquet`, clave `(naics, metric, year)`. Se reescribe
completa en cada corrida (idempotente). Una métrica = una fuente; cuando dos fuentes miden lo
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
    dest = root / "market" / "naics_metrics"
    dest.mkdir(parents=True, exist_ok=True)
    tmp = dest / ".part-0.parquet.tmp"
    pq.write_table(table, tmp)
    tmp.replace(dest / "part-0.parquet")
    metrics = sorted({r["metric"] for r in rows})
    return {"rows": len(rows), "naics": len({r["naics"] for r in rows}), "metrics": metrics}


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
