"""Capa común de las fuentes de la Fase 4 (BLS, BEA, Census): contrato, catálogo y descarga.

Cada fuente implementa un `SourceAdapter` (ver ADR 0005) que solo sabe hablar con su API y
traducir sus metadatos a un catálogo neutral. Todo lo demás es común y vive aquí:

- `crawl`: recorre las unidades de la fuente (encuesta, tabla, dataset), clasifica cada serie con
  las mismas reglas neutrales del embudo (`src/eligibility.py`), escribe el catálogo interno,
  `series_meta` y el linaje. Una unidad cuya versión no cambió no se vuelve a procesar, salvo
  en el refresco completo cada `catalog_refresh_days`.
- `download`: baja los valores vigentes (`vintage_date = NULL`) de las series de los grupos de
  `download.groups`, unidad por unidad y con estado por unidad, así que es reanudable e
  incremental: una unidad cuya versión de valores no cambió no se vuelve a pedir.
- `update`: `crawl` incremental + `download` (lo que corre la actualización semanal).

Nada aquí conoce temas, encuestas ni tablas concretas (principio 1): las unidades salen del
catálogo de cada fuente.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import duckdb
import pyarrow as pa

from src.config import REPO_ROOT, load_config
from src.ingest.store import UpsertResult, read_table, upsert
from src.log import get_logger, run
from src.schemas import ELIGIBILITY, SERIES, SERIES_META, TableSpec

# Código de salida cuando una corrida acotada deja trabajo pendiente (igual que fred.py).
EXIT_PENDING = 10
# Días por periodo, para estimar observaciones a partir de las fechas.
PERIOD_DAYS = {"D": 7 / 5, "W": 7.0, "M": 30.44, "Q": 91.31, "A": 365.25}
# Bytes por fila en Parquet (medido en FRED, ADR 0002).
PARQUET_BYTES_PER_ROW = 15
# Prefijo del series_id de una fila de linaje agregada por unidad (ver `aggregate_lineage`).
UNIT_WILDCARD = "/*"

# Catálogo neutral: lo que cada adaptador sabe de cada serie, en campos sin tema.
SOURCE_CATALOG = TableSpec(
    name="source_catalog",
    schema=pa.schema(
        [
            pa.field("source", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=False),
            pa.field("unit", pa.string(), nullable=False),  # unidad de descarga
            pa.field("title", pa.string()),
            pa.field("frequency", pa.string()),  # código del esquema (D/W/M/Q/A) o NULL
            pa.field("frequency_raw", pa.string()),  # código original de la fuente
            pa.field("units", pa.string()),
            pa.field("seasonal_adjustment", pa.string()),  # SA, NSA, SAAR... o NULL
            pa.field("observation_start", pa.date32()),
            pa.field("observation_end", pa.date32()),
            pa.field("last_updated", pa.timestamp("s", tz="UTC")),
            pa.field("discontinued", pa.bool_(), nullable=False),
            pa.field("copyrighted", pa.bool_(), nullable=False),
            pa.field("geo_level", pa.string()),  # nacional/regional/internacional/sin_geografia
            pa.field("geo_detail", pa.string()),
            pa.field("category_path", pa.string()),
        ]
    ),
    key=("source", "series_id"),
)

# Estado por unidad: versión del catálogo procesada y versión de valores descargada.
UNIT_STATE = TableSpec(
    name="source_unit_state",
    schema=pa.schema(
        [
            pa.field("source", pa.string(), nullable=False),
            pa.field("unit", pa.string(), nullable=False),
            pa.field("kind", pa.string(), nullable=False),  # catalog | values
            pa.field("version", pa.string()),
            pa.field("done_at", pa.timestamp("s", tz="UTC"), nullable=False),
            pa.field("n_series", pa.int64(), nullable=False),
            pa.field("n_rows", pa.int64(), nullable=False),
        ]
    ),
    key=("source", "unit", "kind"),
)

GEO_LEVELS = ("nacional", "regional", "internacional", "sin_geografia")


@dataclass(frozen=True)
class Unit:
    """Unidad de descarga de una fuente: encuesta BLS, tabla BEA, dataset Census."""

    unit_id: str
    # Versión del catálogo de la unidad (ETag, fecha de actualización...). None: desconocida,
    # se reprocesa siempre.
    catalog_version: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


class SourceAdapter(Protocol):
    """Lo único que cada fuente implementa."""

    source: str

    def list_units(self) -> list[Unit]:
        """Todas las unidades de la fuente, recorriendo su catálogo (sin selección temática)."""
        ...

    def catalog_rows(self, unit: Unit) -> Iterator[dict[str, Any]]:
        """Filas del catálogo neutral (`SOURCE_CATALOG`, sin `source`) de la unidad."""
        ...

    def values_version(self, unit: Unit) -> str | None:
        """Versión actual de los valores de la unidad (None: desconocida, se baja siempre)."""
        ...

    def fetch_values(self, unit: Unit, series_ids: set[str]) -> Iterator[dict[str, Any]]:
        """Filas de `series` (sin `source`) de las series pedidas, valores vigentes."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SourcePaths:
    processed: Path
    raw: Path
    source: str

    @property
    def series(self) -> Path:
        return self.processed / "series"

    @property
    def series_meta(self) -> Path:
        return self.processed / "series_meta"

    @property
    def catalog(self) -> Path:
        return self.processed / "_internal" / "source_catalog"

    @property
    def unit_state(self) -> Path:
        return self.processed / "_internal" / "source_unit_state"

    @property
    def eligibility(self) -> Path:
        return self.processed / "lineage" / "eligibility"

    @property
    def cursors(self) -> Path:
        return self.processed / "ingest_state" / f"{self.source}_cursors.json"

    @property
    def initial_done(self) -> Path:
        return self.processed / "ingest_state" / f"{self.source}_initial.done"


def paths_for(cfg: dict[str, Any], source: str) -> SourcePaths:
    return SourcePaths(
        resolve(cfg["paths"]["processed_dir"]), resolve(cfg["paths"]["raw_dir"]), source
    )


def resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


# --- ingestor común ---------------------------------------------------------------------------


class SourceIngestor:
    """crawl / download / update sobre un `SourceAdapter`."""

    def __init__(
        self,
        adapter: SourceAdapter,
        cfg: dict[str, Any],
        paths: SourcePaths,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.adapter = adapter
        self.source = adapter.source
        self.cfg = cfg
        self.scfg = cfg["sources"][self.source]
        self.n_buckets = cfg["sources"]["n_buckets"]
        self.paths = paths
        self._now = now
        self._clock = clock
        self.log = logger or logging.getLogger(__name__)

    # --- crawl + embudo ----------------------------------------------------------------------

    def crawl(self, *, full: bool | None = None, run_id: str = "manual") -> dict[str, Any]:
        """Catálogo, series_meta y linaje de las unidades nuevas o cambiadas.

        Con `full` (o si toca el refresco periódico) reprocesa todas las unidades: la regla de
        inactividad depende de la fecha de hoy, no solo del archivo.
        """
        from src.eligibility import classify_rows  # import tardío: eligibility importa fred

        if full is None:
            full = self.is_stale()
        units = self.adapter.list_units()
        state = self._unit_state("catalog")
        today = self._now().date()
        decided_at = self._now().replace(microsecond=0)
        stats: Counter[str] = Counter(units=len(units))
        for unit in units:
            prev = state.get(unit.unit_id)
            if (
                not full
                and prev is not None
                and unit.catalog_version is not None
                and prev["version"] == unit.catalog_version
            ):
                stats["units_unchanged"] += 1
                continue
            rows = [
                {**r, "source": self.source, "unit": unit.unit_id}
                for r in self.adapter.catalog_rows(unit)
            ]
            decisions = classify_rows(rows, self.cfg, today)
            lineage = aggregate_lineage(decisions, unit.unit_id, self.scfg)
            kept = {d["series_id"] for d in decisions if d["rule"] not in ROW_RULES}
            self._write_catalog([r for r in rows if r["series_id"] in kept])
            self._write_lineage(lineage, run_id, decided_at)
            self._set_state(unit.unit_id, "catalog", unit.catalog_version, len(rows), len(lineage))
            stats["units_processed"] += 1
            stats["series"] += len(rows)
            stats["lineage_rows"] += len(lineage)
            for d in decisions:
                stats[f"grupo_{d['group_name']}"] += 1
        self._save_cursor("last_crawl_ok")
        out = dict(stats)
        self.log.info("crawl terminado", extra={"fields": {"source": self.source, **out}})
        return out

    def is_stale(self) -> bool:
        last = _parse_dt(self._load_cursors().get("last_crawl_ok"))
        return last is None or self._now() - last > timedelta(
            days=self.scfg["catalog_refresh_days"]
        )

    # --- descarga ------------------------------------------------------------------------------

    def download(
        self, *, max_units: int | None = None, budget_s: float | None = None
    ) -> dict[str, Any]:
        """Valores vigentes de las series objetivo, unidad por unidad. Reanudable."""
        targets = self.targets()
        by_unit: dict[str, set[str]] = {}
        for sid, unit in targets.items():
            by_unit.setdefault(unit, set()).add(sid)
        units = {u.unit_id: u for u in self.adapter.list_units()}
        state = self._unit_state("values")
        t0 = self._clock()
        stats: Counter[str] = Counter(target_series=len(targets), target_units=len(by_unit))
        total = UpsertResult()
        pending = 0
        for unit_id in sorted(by_unit):
            unit = units.get(unit_id)
            if unit is None:
                stats["units_missing"] += 1
                continue
            version = self.adapter.values_version(unit)
            prev = state.get(unit_id)
            if (
                prev is not None
                and version is not None
                and prev["version"] == version
                and prev["n_series"] == len(by_unit[unit_id])
            ):
                stats["units_unchanged"] += 1
                continue
            out_of_budget = budget_s is not None and self._clock() - t0 > budget_s
            if (max_units is not None and stats["units_fetched"] >= max_units) or out_of_budget:
                pending += 1
                continue
            res, n_rows = self._fetch_unit(unit, by_unit[unit_id])
            total += res
            self._set_state(unit_id, "values", version, len(by_unit[unit_id]), n_rows)
            stats["units_fetched"] += 1
        stats["units_pending"] = pending
        if pending == 0:
            self.paths.initial_done.parent.mkdir(parents=True, exist_ok=True)
            self.paths.initial_done.write_text(self._now().isoformat(), encoding="utf-8")
        out = {
            **stats,
            "rows_inserted": total.inserted,
            "rows_replaced": total.replaced,
            "rows_unchanged": total.unchanged,
        }
        self.log.info("download terminado", extra={"fields": {"source": self.source, **out}})
        return out

    def _fetch_unit(self, unit: Unit, series_ids: set[str]) -> tuple[UpsertResult, int]:
        res = UpsertResult()
        n_rows = 0
        batch: list[dict[str, Any]] = []
        size = self.scfg.get("write_batch_rows", 500_000)
        seen: set[tuple[str, date]] = set()
        for r in self.adapter.fetch_values(unit, series_ids):
            if r["series_id"] not in series_ids:
                continue
            key = (r["series_id"], r["date"])
            if key in seen:  # la misma observación en dos archivos de una unidad
                continue
            seen.add(key)
            batch.append({**r, "source": self.source, "vintage_date": None})
            if len(batch) >= size:
                res += self._write_values(batch)
                n_rows += len(batch)
                batch = []
        if batch:
            res += self._write_values(batch)
            n_rows += len(batch)
        return res, n_rows

    def update(self, *, budget_s: float | None = None) -> dict[str, Any]:
        """Actualización semanal: crawl incremental (o completo si toca) y descarga."""
        return {"crawl": self.crawl(), "download": self.download(budget_s=budget_s)}

    def targets(self) -> dict[str, str]:
        """series_id -> unidad de las series de los grupos de `download.groups`."""
        groups = list(self.cfg["download"]["groups"])
        decisions = latest_decisions(self.paths, self.source)
        ids = [d["series_id"] for d in decisions if d["group_name"] in groups]
        if not ids:
            return {}
        cat = read_table(
            SOURCE_CATALOG,
            self.paths.catalog,
            source=self.source,
            where="list_contains(?, series_id)",
            params=[ids],
        )
        return dict(
            zip(cat.column("series_id").to_pylist(), cat.column("unit").to_pylist(), strict=True)
        )

    def estimate(self) -> dict[str, Any]:
        """Embudo de la fuente: series por regla y grupo, y tamaño estimado por grupo."""
        decisions = latest_decisions(self.paths, self.source)
        cat = {
            r["series_id"]: r
            for r in read_table(SOURCE_CATALOG, self.paths.catalog, source=self.source)
            .select(["series_id", "frequency", "observation_start", "observation_end", "unit"])
            .to_pylist()
        }
        return funnel(decisions, cat)

    # --- E/S -----------------------------------------------------------------------------------

    def _write_catalog(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        table = pa.Table.from_pylist(rows, schema=SOURCE_CATALOG.schema)
        upsert(
            table, SOURCE_CATALOG, self.paths.catalog, source=self.source, n_buckets=self.n_buckets
        )
        upsert(
            to_series_meta(table),
            SERIES_META,
            self.paths.series_meta,
            source=self.source,
            n_buckets=self.n_buckets,
        )

    def _write_lineage(
        self, decisions: list[dict[str, Any]], run_id: str, decided_at: datetime
    ) -> None:
        if not decisions:
            return
        rows = [
            {**d, "run_id": run_id, "source": self.source, "decided_at": decided_at}
            for d in decisions
        ]
        upsert(
            pa.Table.from_pylist(rows, schema=ELIGIBILITY.schema),
            ELIGIBILITY,
            self.paths.eligibility,
            source=self.source,
            n_buckets=self.n_buckets,
        )

    def _write_values(self, rows: list[dict[str, Any]]) -> UpsertResult:
        return upsert(
            pa.Table.from_pylist(rows, schema=SERIES.schema),
            SERIES,
            self.paths.series,
            source=self.source,
            n_buckets=self.n_buckets,
        )

    def _unit_state(self, kind: str) -> dict[str, dict[str, Any]]:
        t = read_table(
            UNIT_STATE, self.paths.unit_state, source=self.source, where="kind = ?", params=[kind]
        )
        return {r["unit"]: r for r in t.to_pylist()}

    def _set_state(
        self, unit: str, kind: str, version: str | None, n_series: int, n_rows: int
    ) -> None:
        row = {
            "source": self.source,
            "unit": unit,
            "kind": kind,
            "version": version,
            "done_at": self._now().replace(microsecond=0),
            "n_series": n_series,
            "n_rows": n_rows,
        }
        upsert(
            pa.Table.from_pylist([row], schema=UNIT_STATE.schema),
            UNIT_STATE,
            self.paths.unit_state,
            source=self.source,
            n_buckets=self.n_buckets,
            bucket_col="unit",
        )

    def _save_cursor(self, name: str) -> None:
        cursors = self._load_cursors()
        cursors[name] = self._now().isoformat()
        path = self.paths.cursors
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cursors, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _load_cursors(self) -> dict[str, Any]:
        path = self.paths.cursors
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# --- funciones puras ----------------------------------------------------------------------------

# Reglas que se deciden mirando una sola fila del catálogo (sin compararla con otras series).
ROW_RULES = ("descontinuada", "frecuencia_no_soportada", "sin_fechas", "inactiva")


def aggregate_lineage(
    decisions: list[dict[str, Any]], unit_id: str, scfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """Linaje de una unidad: una fila por serie, o una fila agregada si toda la unidad cae por
    la misma regla de fila (p. ej. una encuesta entera sin datos recientes) y supera
    `lineage_aggregate_min_series`. La fila agregada lleva `series_id = <unidad>/*` y el número
    de series en `detail`; las series se reconstruyen desde la unidad versionada.
    """
    threshold = scfg.get("lineage_aggregate_min_series")
    rules = {d["rule"] for d in decisions}
    if (
        threshold is None
        or len(decisions) < threshold
        or len(rules) != 1
        or next(iter(rules)) not in ROW_RULES
    ):
        return decisions
    rule = next(iter(rules))
    details = sorted(d["detail"] for d in decisions if d["detail"])
    freqs = Counter(d["frequency"] for d in decisions)
    return [
        {
            "series_id": f"{unit_id}{UNIT_WILDCARD}",
            "group_name": "excluida",
            "rule": rule,
            "detail": f"n_series={len(decisions)};max={details[-1] if details else ''}",
            "geo_level": None,
            "frequency": freqs.most_common(1)[0][0],
            "copyrighted": any(d["copyrighted"] for d in decisions),
        }
    ]


def lineage_series_count(decision: dict[str, Any]) -> int:
    """Series que representa una fila de linaje (1, o n_series si es agregada)."""
    if not decision["series_id"].endswith(UNIT_WILDCARD):
        return 1
    for part in (decision["detail"] or "").split(";"):
        if part.startswith("n_series="):
            return int(part.removeprefix("n_series="))
    return 1


def latest_decisions(paths: SourcePaths, source: str) -> list[dict[str, Any]]:
    """Última decisión de cada serie (o unidad agregada) de la fuente.

    Como `crawl` solo reprocesa las unidades que cambiaron, la última decisión de cada serie
    puede venir de corridas distintas: se toma la más reciente por serie.
    """
    files = sorted((paths.eligibility / source).glob("??/*.parquet"))
    if not files:
        return []
    cols = ", ".join(ELIGIBILITY.schema.names)
    con = duckdb.connect()
    try:
        t = con.execute(
            f"SELECT {cols} FROM read_parquet(?) "
            "QUALIFY row_number() OVER (PARTITION BY series_id "
            "ORDER BY decided_at DESC, run_id DESC) = 1 ORDER BY series_id",
            [[str(f) for f in files]],
        ).to_arrow_table()
    finally:
        con.close()
    return t.to_pylist()


def funnel(decisions: Iterable[dict[str, Any]], catalog: dict[str, dict[str, Any]]) -> dict:
    """Conteos por regla y grupo (contando las filas agregadas por su n_series) y tamaño."""
    by_rule: Counter[str] = Counter()
    by_group: Counter[str] = Counter()
    sizes: dict[str, Counter[str]] = {}
    for d in decisions:
        n = lineage_series_count(d)
        by_group[d["group_name"]] += n
        if d["group_name"] == "excluida":
            by_rule[d["rule"]] += n
            continue
        s = sizes.setdefault(d["group_name"], Counter())
        s["series"] += n
        r = catalog.get(d["series_id"])
        if r:
            s["obs_estimadas"] += est_obs(r)
    units_by_group: dict[str, set[str]] = {}
    for d in decisions:
        r = catalog.get(d["series_id"])
        if r and d["group_name"] != "excluida":
            units_by_group.setdefault(d["group_name"], set()).add(r["unit"])
    tam = {
        g: {
            "series": s["series"],
            "unidades": len(units_by_group.get(g, ())),
            "obs_estimadas": s["obs_estimadas"],
            "parquet_mb": round(s["obs_estimadas"] * PARQUET_BYTES_PER_ROW / 1e6, 1),
        }
        for g, s in sizes.items()
    }
    return {
        "total": sum(by_group.values()),
        "excluidas_por_regla": dict(by_rule.most_common()),
        "grupos": dict(by_group.most_common()),
        "tamano_por_grupo": tam,
    }


def est_obs(r: dict[str, Any]) -> int:
    if not r.get("observation_start") or not r.get("observation_end") or not r.get("frequency"):
        return 0
    days = (r["observation_end"] - r["observation_start"]).days
    return int(days / PERIOD_DAYS[r["frequency"]]) + 1


def to_series_meta(catalog: pa.Table) -> pa.Table:
    cols = {
        name: catalog.column(name)
        for name in SERIES_META.schema.names
        if name not in ("start_date", "end_date")
    }
    cols["start_date"] = catalog.column("observation_start")
    cols["end_date"] = catalog.column("observation_end")
    return pa.table({n: cols[n] for n in SERIES_META.schema.names}, schema=SERIES_META.schema)


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# --- CLI común ----------------------------------------------------------------------------------


def main(
    argv: list[str] | None,
    source: str,
    make_adapter: Callable[[dict[str, Any], SourcePaths, logging.Logger], SourceAdapter],
) -> int:
    """`python -m src.ingest.<fuente> crawl [--full] | download | update | estimate`."""
    parser = argparse.ArgumentParser(prog=f"python -m src.ingest.{source}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    crawl = sub.add_parser("crawl", help="catálogo, series_meta y embudo (unidades cambiadas)")
    crawl.add_argument("--full", action="store_true", help="reprocesar todas las unidades")
    crawl.add_argument(
        "--if-stale",
        action="store_true",
        help="solo si el último crawl completo es más viejo que el refresco",
    )
    dl = sub.add_parser("download", help="valores vigentes de las series objetivo")
    dl.add_argument("--max-units", type=int, default=None)
    dl.add_argument("--budget-min", type=float, default=None)
    up = sub.add_parser("update", help="crawl incremental + download (corrida semanal)")
    up.add_argument("--budget-min", type=float, default=None)
    sub.add_parser("estimate", help="embudo y tamaño estimado por grupo")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    cfg = load_config()
    log = get_logger(f"ingest.{source}", cfg["logging"]["level"])
    paths = paths_for(cfg, source)
    if args.cmd == "estimate":
        ing = SourceIngestor(_NullAdapter(source), cfg, paths, logger=log)
        print(json.dumps({"source": source, **ing.estimate()}, indent=2, ensure_ascii=False))
        return 0
    adapter = make_adapter(cfg, paths, log)
    ing = SourceIngestor(adapter, cfg, paths, logger=log)
    budget = getattr(args, "budget_min", None)
    budget_s = budget * 60 if budget is not None else None
    try:
        with run([source], resolve(cfg["logging"]["runs_file"])) as record:
            with record.stage(args.cmd):
                if args.cmd == "crawl":
                    if args.if_stale and not ing.is_stale():
                        stats: dict[str, Any] = {"skipped": "catálogo al día"}
                    else:
                        stats = ing.crawl(full=True if args.full else None, run_id=record.run_id)
                elif args.cmd == "download":
                    stats = ing.download(max_units=args.max_units, budget_s=budget_s)
                else:
                    stats = {
                        "crawl": ing.crawl(run_id=record.run_id),
                        "download": ing.download(budget_s=budget_s),
                    }
            record.n_series[source] = int(
                stats.get("series", stats.get("crawl", {}).get("series", 0))
                if isinstance(stats.get("crawl", {}), dict)
                else 0
            )
            record.details = {"command": args.cmd, **stats}
    finally:
        adapter.close()
    print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
    dl_stats = stats.get("download", stats) if isinstance(stats, dict) else {}
    return EXIT_PENDING if dl_stats.get("units_pending") else 0


class _NullAdapter:
    def __init__(self, source: str) -> None:
        self.source = source

    def close(self) -> None:  # pragma: no cover - trivial
        pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(2)
