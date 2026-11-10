"""Capa común de las fuentes de la Fase 4 (BLS, BEA, Census): contrato, catálogo y descarga.

Cada fuente implementa un `SourceAdapter` (ver ADR 0007) que solo sabe hablar con su API y
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
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from src.config import REPO_ROOT, load_config
from src.ingest.store import UpsertResult, bucket_of, read_table, upsert
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
        sleep: Callable[[float], None] = time.sleep,
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
        self._sleep = sleep
        self.log = logger or logging.getLogger(__name__)

    # --- crawl + embudo ----------------------------------------------------------------------

    def crawl(
        self, *, full: bool = False, run_id: str = "manual", budget_s: float | None = None
    ) -> dict[str, Any]:
        """Catálogo, series_meta y linaje de las unidades que lo necesitan.

        Una unidad se reprocesa si no tiene estado, si su versión cambió o es desconocida, o si
        su última clasificación tiene más de `catalog_refresh_days` (la regla de inactividad
        depende de la fecha de hoy, no solo del archivo). Con `full`, todas. Con `budget_s`,
        corta entre unidades al pasar el presupuesto: lo pendiente sigue en la próxima corrida.
        """
        units = self.adapter.list_units()
        state = self._unit_state("catalog")
        today = self._now().date()
        decided_at = self._now().replace(microsecond=0)
        refresh = timedelta(days=self.scfg["catalog_refresh_days"])
        t0 = self._clock()
        stats: Counter[str] = Counter(units=len(units))
        for unit in units:
            prev = state.get(unit.unit_id)
            if (
                not full
                and prev is not None
                and unit.catalog_version is not None
                and prev["version"] == unit.catalog_version
                and self._now() - prev["done_at"] <= refresh
            ):
                stats["units_unchanged"] += 1
                continue
            if budget_s is not None and self._clock() - t0 > budget_s:
                stats["units_pending"] += 1
                continue
            got = self._retrying(
                unit, lambda u=unit: self._crawl_unit(u, run_id, decided_at, today)
            )
            if got is None:
                stats["units_failed"] += 1
                continue
            stats += got
            stats["units_processed"] += 1
        if not stats["units_pending"] and not stats["units_failed"]:
            self._save_cursor("last_crawl_ok")
        out = dict(stats)
        self.log.info("crawl terminado", extra={"fields": {"source": self.source, **out}})
        return out

    def _retrying(self, unit: Unit, fn: Callable[[], Any]) -> Any:
        """Corre `fn` reintentando si la conexión se corta a mitad de un archivo.

        `http.stream` reintenta solo el inicio de la respuesta; un corte posterior repite la
        unidad entera (las escrituras son upserts por clave, así que repetir es idempotente).
        Si se agotan los reintentos devuelve None: la unidad queda pendiente y la fuente sigue.
        """
        tries = self.scfg["max_retries"]
        for attempt in range(tries + 1):
            try:
                return fn()
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                fields = {"source": self.source, "unit": unit.unit_id, "error": type(exc).__name__}
                if attempt == tries:
                    self.log.warning("unidad fallida; queda pendiente", extra={"fields": fields})
                    return None
                self.log.warning("corte de conexión; se repite la unidad", extra={"fields": fields})
                self._sleep(self.scfg["backoff_base_s"] * 2**attempt)
        return None

    def _crawl_unit(
        self, unit: Unit, run_id: str, decided_at: datetime, today: date
    ) -> Counter[str]:
        """Clasifica una unidad con memoria acotada.

        Las reglas de fila se aplican en streaming; las series que siguen en el embudo se
        reparten por título normalizado (la regla de variantes solo compara títulos iguales) y,
        si la unidad es grande, se derraman a disco por fragmentos.
        """
        from src.eligibility import classify_rows, normalize_title, row_rule

        max_stale = self.cfg["eligibility"]["max_staleness_days"]
        threshold = self.scfg.get("lineage_aggregate_min_series")
        spill_rows = self.scfg.get("spill_rows", SPILL_ROWS)
        stats: Counter[str] = Counter()
        self.paths.raw.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.paths.raw, prefix=".crawl-") as tmp:
            excluded = _Spill(Path(tmp) / "excluded", _DECISION_SCHEMA, spill_rows)
            active = _Spill(
                Path(tmp) / "active",
                SOURCE_CATALOG.schema,
                spill_rows,
                shard_key=lambda r: normalize_title(r["title"]),
            )
            rules: Counter[str] = Counter()
            max_detail: dict[str, str] = {}
            for raw in self.adapter.catalog_rows(unit):
                r = {**raw, "source": self.source, "unit": unit.unit_id}
                d = row_rule(r, max_stale, today)
                if d is None:
                    active.add(r)
                    continue
                excluded.add(d)
                rules[d["rule"]] += 1
                if d["detail"] and d["detail"] > max_detail.get(d["rule"], ""):
                    max_detail[d["rule"]] = d["detail"]
            n = excluded.n + active.n
            stats["series"] += n
            for rule, k in rules.items():
                stats[f"regla_{rule}"] += k
            if active.n == 0 and len(rules) == 1 and threshold is not None and n >= threshold:
                # Toda la unidad cae por la misma regla de fila: una fila agregada (ADR 0007).
                rule = next(iter(rules))
                freq = next(excluded.rows())["frequency"]
                lineage = [
                    {
                        "series_id": f"{unit.unit_id}{UNIT_WILDCARD}",
                        "group_name": "excluida",
                        "rule": rule,
                        "detail": (
                            f"n_series={n};max={max_detail.get(rule, '')};"
                            f"version={unit.catalog_version or ''}"
                        ),
                        "geo_level": None,
                        "frequency": freq,
                        "copyrighted": False,
                    }
                ]
                self._write_lineage(lineage, run_id, decided_at)
                stats["lineage_rows"] += 1
                stats["units_aggregated"] += 1
            else:
                # Se escribe en lotes grandes (write_batch_rows): pocos archivos por bucket.
                size = self.scfg["write_batch_rows"]
                lineage_buf: list[dict[str, Any]] = []
                catalog_buf: list[dict[str, Any]] = []

                def flush(force: bool = False) -> None:
                    if lineage_buf and (force or len(lineage_buf) >= size):
                        self._write_lineage(lineage_buf, run_id, decided_at)
                        stats["lineage_rows"] += len(lineage_buf)
                        lineage_buf.clear()
                    if catalog_buf and (force or len(catalog_buf) >= size):
                        self._write_catalog(catalog_buf)
                        catalog_buf.clear()

                for batch in excluded.batches():
                    lineage_buf.extend(batch)
                    flush()
                for shard in active.batches():
                    decisions = classify_rows(shard, self.cfg, today)
                    lineage_buf.extend(decisions)
                    catalog_buf.extend(shard)
                    for d in decisions:
                        stats[f"grupo_{d['group_name']}"] += 1
                    flush()
                flush(force=True)
            stats["grupo_excluida"] += excluded.n
        self._set_state(unit.unit_id, "catalog", unit.catalog_version, n, stats["lineage_rows"])
        return stats

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
            # El presupuesto se mira antes de `values_version`: en BEA esa versión es una
            # consulta de datos, y sin presupuesto no hay nada que hacer con ella.
            out_of_budget = budget_s is not None and self._clock() - t0 > budget_s
            if (max_units is not None and stats["units_fetched"] >= max_units) or out_of_budget:
                pending += 1
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
            got = self._retrying(unit, lambda u=unit: self._fetch_unit(u, by_unit[u.unit_id]))
            if got is None:
                stats["units_failed"] += 1
                pending += 1
                continue
            res, n_rows = got
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

    def update(self, *, budget_s: float | None = None, run_id: str = "manual") -> dict[str, Any]:
        """Actualización semanal: crawl incremental y descarga, dentro de un mismo presupuesto."""
        t0 = self._clock()
        crawl_budget = budget_s
        if budget_s is not None and self.targets():
            # Con series objetivo ya clasificadas, el crawl no puede comerse todo el presupuesto:
            # si no, una fuente con muchas unidades (BEA) recorre el catálogo cada semana y
            # nunca descarga. Sin objetivos aún, el crawl usa el presupuesto entero.
            crawl_budget = budget_s * self.scfg.get("crawl_budget_share", 0.5)
        crawl = self.crawl(run_id=run_id, budget_s=crawl_budget)
        left = None if budget_s is None else max(0.0, budget_s - (self._clock() - t0))
        return {"crawl": crawl, "download": self.download(budget_s=left)}

    def targets(self) -> dict[str, str]:
        """series_id -> unidad de las series a descargar: los grupos de `download.groups` más las
        series jóvenes (`eligibility.young`, ADR 0017) de nivel geográfico descargable."""
        con = self._lineage_con()
        if con is None:
            return {}
        young = (self.cfg["eligibility"].get("young") or {}).get("groups") or []
        try:
            rows = con.execute(
                "SELECT l.series_id, c.unit FROM latest l JOIN catalog c USING (series_id) "
                "WHERE l.group_name IN (SELECT unnest(?)) "
                "OR (l.group_name IN (SELECT unnest(?)) AND l.geo_level IN (SELECT unnest(?)))",
                [
                    list(self.cfg["download"]["groups"]),
                    list(young),
                    list(self.cfg["download"]["groups"]),
                ],
            ).fetchall()
        finally:
            con.close()
        return dict(rows)

    def estimate(self) -> dict[str, Any]:
        """Embudo de la fuente: series por regla y grupo (las filas agregadas cuentan por su
        n_series) y tamaño estimado de los grupos que entran al análisis."""
        con = self._lineage_con()
        if con is None:
            return {"total": 0, "excluidas_por_regla": {}, "grupos": {}, "tamano_por_grupo": {}}
        n_expr = (
            f"CASE WHEN ends_with(series_id, '{UNIT_WILDCARD}') THEN "
            "coalesce(TRY_CAST(regexp_extract(detail, 'n_series=([0-9]+)', 1) AS BIGINT), 1) "
            "ELSE 1 END"
        )
        days = " ".join(f"WHEN '{f}' THEN {d}" for f, d in PERIOD_DAYS.items())
        try:
            groups = con.execute(
                f"SELECT group_name, sum({n_expr}) FROM latest GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
            rules = con.execute(
                f"SELECT rule, sum({n_expr}) FROM latest WHERE group_name = 'excluida' "
                "GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
            sizes = con.execute(
                "SELECT l.group_name, count(*), count(DISTINCT c.unit), "
                "sum(CAST(floor(date_diff('day', c.observation_start, c.observation_end) / "
                f"(CASE c.frequency {days} END)) AS BIGINT) + 1) "
                "FROM latest l JOIN catalog c USING (series_id) "
                "WHERE l.group_name <> 'excluida' GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()
        finally:
            con.close()
        return {
            "total": int(sum(n for _, n in groups)),
            "excluidas_por_regla": {r: int(n) for r, n in rules},
            "grupos": {g: int(n) for g, n in groups},
            "tamano_por_grupo": {
                g: {
                    "series": int(n),
                    "unidades": int(u),
                    "obs_estimadas": int(obs or 0),
                    "parquet_mb": round((obs or 0) * PARQUET_BYTES_PER_ROW / 1e6, 1),
                }
                for g, n, u, obs in sizes
            },
        }

    def _lineage_con(self) -> duckdb.DuckDBPyConnection | None:
        """Conexión con vistas `latest` (última decisión por serie) y `catalog`."""
        lineage = sorted((self.paths.eligibility / self.source).glob("??/*.parquet"))
        if not lineage:
            return None
        catalog = sorted((self.paths.catalog / self.source).glob("??/*.parquet"))
        con = duckdb.connect()
        con.execute(
            f"CREATE VIEW latest AS SELECT * FROM read_parquet({_sql_list(lineage)}) "
            "QUALIFY row_number() OVER "
            "(PARTITION BY series_id ORDER BY decided_at DESC, run_id DESC) = 1"
        )
        if catalog:
            con.execute(f"CREATE VIEW catalog AS SELECT * FROM read_parquet({_sql_list(catalog)})")
        else:
            con.register("_empty_catalog", SOURCE_CATALOG.schema.empty_table())
            con.execute("CREATE VIEW catalog AS SELECT * FROM _empty_catalog")
        return con

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


# --- derrame a disco ---------------------------------------------------------------------------

SPILL_ROWS = 200_000
SPILL_SHARDS = 64
_DECISION_SCHEMA = pa.schema(
    [
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("group_name", pa.string(), nullable=False),
        pa.field("rule", pa.string(), nullable=False),
        pa.field("detail", pa.string()),
        pa.field("geo_level", pa.string()),
        pa.field("frequency", pa.string()),
        pa.field("copyrighted", pa.bool_(), nullable=False),
    ]
)


class _Spill:
    """Filas en memoria hasta `limit`; después, en Parquet por fragmentos.

    Con `shard_key`, las filas con la misma clave caen siempre en el mismo fragmento, y
    `batches()` devuelve un fragmento completo cada vez (o todo junto si nunca se derramó).
    """

    def __init__(
        self,
        directory: Path,
        schema: pa.Schema,
        limit: int,
        shard_key: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.dir = directory
        self.schema = schema
        self.limit = limit
        self.shard_key = shard_key
        self.n = 0
        self._mem: list[dict[str, Any]] = []
        self._shards: dict[int, list[dict[str, Any]]] = {}
        self._spilled = False

    def add(self, row: dict[str, Any]) -> None:
        self.n += 1
        if not self._spilled:
            self._mem.append(row)
            if len(self._mem) >= self.limit:
                self._spilled = True
                for r in self._mem:
                    self._add_shard(r)
                self._mem = []
            return
        self._add_shard(row)

    def _add_shard(self, row: dict[str, Any]) -> None:
        k = bucket_of(self.shard_key(row), SPILL_SHARDS) if self.shard_key else 0
        buf = self._shards.setdefault(k, [])
        buf.append(row)
        if len(buf) >= max(1, self.limit // 4):
            self._flush(k)

    def _flush(self, k: int) -> None:
        buf = self._shards.get(k)
        if not buf:
            return
        d = self.dir / f"{k:02d}"
        d.mkdir(parents=True, exist_ok=True)
        cols = {name: [r.get(name) for r in buf] for name in self.schema.names}
        pq.write_table(pa.table(cols, schema=self.schema), d / f"{uuid.uuid4().hex}.parquet")
        self._shards[k] = []

    def rows(self) -> Iterator[dict[str, Any]]:
        for batch in self.batches():
            yield from batch

    def batches(self) -> Iterator[list[dict[str, Any]]]:
        if not self._spilled:
            if self._mem:
                yield self._mem
            return
        for k in list(self._shards):
            self._flush(k)
        for d in sorted(self.dir.iterdir()):
            files = sorted(d.glob("*.parquet"))
            if self.shard_key is None:
                for f in files:
                    yield pq.read_table(f).to_pylist()
            else:
                yield pa.concat_tables([pq.read_table(f) for f in files]).to_pylist()


# --- funciones puras ----------------------------------------------------------------------------


def expand_aggregated(
    adapter: SourceAdapter,
    unit: Unit,
    decision: dict[str, Any],
    cfg: dict[str, Any],
    today: date,
) -> list[dict[str, Any]]:
    """Decisiones por serie que resume una fila agregada, reconstruidas desde la unidad.

    Solo es válido si la unidad sigue en la versión registrada (ETag...): si cambió, falla en
    vez de devolver decisiones de otro archivo. `today` es la fecha de la decisión original.
    """
    from src.eligibility import row_rule

    fields = dict(p.split("=", 1) for p in (decision["detail"] or "").split(";") if "=" in p)
    if fields.get("version", "") != (unit.catalog_version or ""):
        raise ValueError(
            f"{unit.unit_id}: versión {unit.catalog_version!r} distinta de la registrada "
            f"{fields.get('version')!r}"
        )
    max_stale = cfg["eligibility"]["max_staleness_days"]
    out = []
    for r in adapter.catalog_rows(unit):
        d = row_rule(r, max_stale, today)
        if d is None or d["rule"] != decision["rule"]:
            raise ValueError(f"{unit.unit_id}: {r['series_id']} no cae en {decision['rule']}")
        out.append(d)
    if len(out) != lineage_series_count(decision):
        raise ValueError(f"{unit.unit_id}: {len(out)} series, la fila dice otra cosa")
    return sorted(out, key=lambda d: d["series_id"])


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


def to_series_meta(catalog: pa.Table) -> pa.Table:
    cols = {
        name: catalog.column(name)
        for name in SERIES_META.schema.names
        if name not in ("start_date", "end_date")
    }
    cols["start_date"] = catalog.column("observation_start")
    cols["end_date"] = catalog.column("observation_end")
    return pa.table({n: cols[n] for n in SERIES_META.schema.names}, schema=SERIES_META.schema)


def _sql_list(files: list[Path]) -> str:
    return "[" + ", ".join("'" + str(f).replace("'", "''") + "'" for f in files) + "]"


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
    crawl = sub.add_parser("crawl", help="catálogo, series_meta y embudo (unidades pendientes)")
    crawl.add_argument("--full", action="store_true", help="reprocesar todas las unidades")
    crawl.add_argument("--budget-min", type=float, default=None)
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
    budget_s = args.budget_min * 60 if args.budget_min is not None else None
    try:
        with run([source], resolve(cfg["logging"]["runs_file"])) as record:
            with record.stage(args.cmd):
                if args.cmd == "crawl":
                    stats: dict[str, Any] = ing.crawl(
                        full=args.full, run_id=record.run_id, budget_s=budget_s
                    )
                elif args.cmd == "download":
                    stats = ing.download(max_units=args.max_units, budget_s=budget_s)
                else:
                    stats = ing.update(budget_s=budget_s, run_id=record.run_id)
            crawl_stats = stats.get("crawl", stats)
            record.n_series[source] = int(crawl_stats.get("series", 0))
            record.details = {"command": args.cmd, **stats}
    finally:
        adapter.close()
    print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
    return EXIT_PENDING if pending(stats) else 0


def pending(stats: dict[str, Any]) -> bool:
    """¿Quedó trabajo para la próxima corrida (presupuesto agotado o unidades con cortes)?"""
    parts = [stats[k] for k in ("crawl", "download") if k in stats] or [stats]
    return any(p.get("units_pending") or p.get("units_failed") for p in parts)


class _NullAdapter:
    def __init__(self, source: str) -> None:
        self.source = source

    def close(self) -> None:  # pragma: no cover - trivial
        pass


if __name__ == "__main__":  # pragma: no cover
    sys.exit(2)
