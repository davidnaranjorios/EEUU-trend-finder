"""Ingesta FRED: catálogo completo, observaciones de series elegibles y actualización incremental.

Decisiones documentadas en docs/decisions/0001-cliente-fred.md. En resumen:
- El catálogo sale de recorrer todo el árbol de categorías; no hay selección temática.
- Las observaciones se piden con el rango realtime completo y se guardan solo las vigentes
  (`realtime_end == 9999-12-31`), con `vintage_date = realtime_start`. Así cada valor lleva la
  fecha en que se publicó y las revisiones posteriores se acumulan como filas nuevas.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
from dotenv import load_dotenv

from src.config import REPO_ROOT, load_config
from src.ingest.http import ApiClient, ApiError
from src.ingest.store import read_table, upsert
from src.log import get_logger, run
from src.schemas import SERIES, SERIES_META, TableSpec

SOURCE = "fred"
BASE_URL = "https://api.stlouisfed.org/fred/"
REALTIME_MIN = "1776-07-04"
REALTIME_MAX = "9999-12-31"
ROOT_CATEGORY = 0
# frequency_short de FRED -> frecuencia del esquema común. Lo demás (BW, SA...) se excluye
# y se cuenta en el log.
FREQ_MAP = {"D": "D", "W": "W", "M": "M", "Q": "Q", "A": "A"}
# Días por periodo, para estimar el número de observaciones a partir de las fechas.
PERIOD_DAYS = {"D": 7 / 5, "W": 7.0, "M": 30.44, "Q": 91.31, "A": 365.25}

# Tablas internas del adaptador (no forman parte del esquema común).
FRED_CATALOG = TableSpec(
    name="fred_catalog",
    schema=pa.schema(
        [
            pa.field("source", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=False),
            pa.field("title", pa.string()),
            pa.field("frequency_short", pa.string()),
            pa.field("units", pa.string()),
            pa.field("seasonal_adjustment", pa.string()),
            pa.field("observation_start", pa.date32()),
            pa.field("observation_end", pa.date32()),
            pa.field("last_updated", pa.timestamp("s", tz="UTC")),
            pa.field("category_ids", pa.list_(pa.int64())),
            pa.field("category_path", pa.string()),
            pa.field("notes_present", pa.bool_(), nullable=False),
            pa.field("discontinued", pa.bool_(), nullable=False),
            pa.field("copyrighted", pa.bool_(), nullable=False),
        ]
    ),
    key=("source", "series_id"),
)

FETCH_STATE = TableSpec(
    name="fred_fetch_state",
    schema=pa.schema(
        [
            pa.field("source", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=False),
            pa.field("last_updated", pa.timestamp("s", tz="UTC")),
            pa.field("fetched_at", pa.timestamp("s", tz="UTC"), nullable=False),
            pa.field("n_rows", pa.int64(), nullable=False),
            # Inicio del rango realtime usado cuando la serie excede el tope de vintages.
            pa.field("realtime_floor", pa.date32()),
        ]
    ),
    key=("source", "series_id"),
)


@dataclass(frozen=True)
class Paths:
    processed: Path
    raw: Path

    @property
    def series(self) -> Path:
        return self.processed / "series"

    @property
    def series_meta(self) -> Path:
        return self.processed / "series_meta"

    @property
    def catalog(self) -> Path:
        return self.processed / "_internal" / "fred_catalog"

    @property
    def fetch_state(self) -> Path:
        return self.processed / "ingest_state" / "fred_fetch"

    @property
    def cursors(self) -> Path:
        return self.processed / "ingest_state" / "fred_cursors.json"


class FredIngestor:
    """Orquesta crawl, observations y update sobre un `ApiClient`."""

    def __init__(
        self,
        client: ApiClient,
        cfg: dict[str, Any],
        paths: Paths,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.fred = cfg["fred"]
        self.paths = paths
        self._now = now
        self.log = logger or logging.getLogger(__name__)

    @classmethod
    def from_config(
        cls, cfg: dict[str, Any], api_key: str, logger: logging.Logger | None = None
    ) -> FredIngestor:
        fred = cfg["fred"]
        paths = Paths(_resolve(cfg["paths"]["processed_dir"]), _resolve(cfg["paths"]["raw_dir"]))
        client = ApiClient(
            BASE_URL,
            {"api_key": api_key},
            requests_per_minute=fred["requests_per_minute"],
            max_retries=fred["max_retries"],
            timeout_s=fred["timeout_s"],
            backoff_base_s=fred["backoff_base_s"],
            raw_dir=paths.raw / SOURCE,
            logger=logger,
        )
        return cls(client, cfg, paths, logger=logger)

    # --- crawl ---------------------------------------------------------------------------------

    def crawl(self) -> dict[str, int]:
        """Recorre todo el árbol de categorías y actualiza fred_catalog y series_meta."""
        infos: dict[str, dict[str, Any]] = {}
        cats_of: dict[str, set[int]] = defaultdict(set)
        paths_of: dict[str, set[str]] = defaultdict(set)
        queue: deque[tuple[int, str]] = deque([(ROOT_CATEGORY, "")])
        seen: set[int] = set()
        while queue:
            cid, path = queue.popleft()
            if cid in seen:
                continue
            seen.add(cid)
            children = self._get("category/children", {"category_id": cid}, use_cache=True)
            for child in children["categories"]:
                child_path = f"{path} > {child['name']}" if path else child["name"]
                queue.append((child["id"], child_path))
            for info in self._paged(
                "category/series",
                {"category_id": cid},
                "seriess",
                self.fred["page_limit_series"],
                use_cache=True,
            ):
                infos[info["id"]] = info
                cats_of[info["id"]].add(cid)
                paths_of[info["id"]].add(path)

        rows = [catalog_row(info, cats_of[sid], paths_of[sid]) for sid, info in infos.items()]
        self._write_catalog(rows)
        self._save_cursor("last_crawl_ok")
        stats = {
            "categories": len(seen),
            "series": len(rows),
            "notes_missing": sum(not r["notes_present"] for r in rows),
            "copyrighted": sum(r["copyrighted"] for r in rows),
            "discontinued": sum(r["discontinued"] for r in rows),
        }
        self.log.info("crawl terminado", extra={"fields": stats})
        return stats

    # --- observations --------------------------------------------------------------------------

    def observations(
        self, category_id: int | None = None, max_series: int | None = None
    ) -> dict[str, Any]:
        """Descarga observaciones de las series elegibles nuevas o actualizadas (reanudable)."""
        catalog = read_table(FRED_CATALOG, self.paths.catalog, source=SOURCE).to_pylist()
        if category_id is not None:
            catalog = [r for r in catalog if category_id in (r["category_ids"] or [])]
        eligible, counts = eligibility(catalog, self.cfg, self._now().date())
        state = {
            r["series_id"]: r
            for r in read_table(FETCH_STATE, self.paths.fetch_state, source=SOURCE).to_pylist()
        }
        todo = sorted(
            (r for r in eligible if _needs_fetch(r, state.get(r["series_id"]))),
            key=lambda r: r["series_id"],
        )
        if max_series is not None:
            todo = todo[:max_series]

        # Contadores: fetched, rows_*, vintage_cap_fallbacks, series_errors.
        tally: Counter[str] = Counter()
        batch_size = self.fred["obs_batch_size"]
        for i in range(0, len(todo), batch_size):
            series_rows: list[dict[str, Any]] = []
            state_rows: list[dict[str, Any]] = []
            for r in todo[i : i + batch_size]:
                try:
                    rows, floor, fell_back = self._fetch_series(r, state.get(r["series_id"]))
                except ApiError as exc:
                    if exc.status is None:  # reintentos agotados: abortar la corrida
                        raise
                    tally["series_errors"] += 1
                    self.log.warning(
                        "serie omitida por error de la API",
                        extra={"fields": {"series_id": r["series_id"], "error": str(exc)}},
                    )
                    continue
                tally["vintage_cap_fallbacks"] += fell_back
                series_rows.extend(rows)
                state_rows.append(
                    {
                        "source": SOURCE,
                        "series_id": r["series_id"],
                        "last_updated": r["last_updated"],
                        "fetched_at": self._now().replace(microsecond=0),
                        "n_rows": len(rows),
                        "realtime_floor": floor,
                    }
                )
            # Primero los datos y después el estado: si algo se corta, se vuelve a pedir.
            res = upsert(
                pa.Table.from_pylist(series_rows, schema=SERIES.schema),
                SERIES,
                self.paths.series,
                source=SOURCE,
                n_buckets=self.fred["n_buckets"],
            )
            upsert(
                pa.Table.from_pylist(state_rows, schema=FETCH_STATE.schema),
                FETCH_STATE,
                self.paths.fetch_state,
                source=SOURCE,
                n_buckets=self.fred["n_buckets"],
            )
            tally["fetched"] += len(state_rows)
            tally["rows_inserted"] += res.inserted
            tally["rows_unchanged"] += res.unchanged
            tally["rows_replaced"] += res.replaced
            self.log.info(
                "lote de observaciones",
                extra={"fields": {"done": tally["fetched"], "of": len(todo)}},
            )
        if tally["rows_replaced"]:
            self.log.warning(
                "claves reescritas con valores distintos",
                extra={"fields": {"rows_replaced": tally["rows_replaced"]}},
            )
        stats: dict[str, Any] = {"eligibility": counts, "to_fetch": len(todo), **tally}
        for key in ("fetched", "series_errors", "vintage_cap_fallbacks"):
            stats.setdefault(key, 0)
        self.log.info("observaciones terminadas", extra={"fields": stats})
        return stats

    def _fetch_series(
        self, r: dict[str, Any], st: dict[str, Any] | None
    ) -> tuple[list[dict[str, Any]], date | None, bool]:
        sid = r["series_id"]
        floor: date | None = st["realtime_floor"] if st else None
        fell_back = False
        try:
            obs = self._observations(sid, floor)
        except ApiError as exc:
            if floor is not None or exc.status != 400 or "vintage" not in str(exc).lower():
                raise
            # La serie excede el tope de vintages por consulta: se pide desde su última
            # vintage. Las observaciones viejas quedan fechadas en esa vintage (más tarde que
            # su publicación real), lo que nunca introduce información del futuro.
            vd = self._get(
                "series/vintagedates", {"series_id": sid, "sort_order": "desc", "limit": 1}
            )
            floor = date.fromisoformat(vd["vintage_dates"][0])
            fell_back = True
            self.log.warning(
                "tope de vintages: se usa realtime_floor",
                extra={"fields": {"series_id": sid, "realtime_floor": floor.isoformat()}},
            )
            obs = self._observations(sid, floor)

        freq = FREQ_MAP[r["frequency_short"]]
        rows = [
            {
                "source": SOURCE,
                "series_id": sid,
                "date": date.fromisoformat(o["date"]),
                "value": None if o["value"] == "." else float(o["value"]),
                "vintage_date": date.fromisoformat(o["realtime_start"]),
                "frequency": freq,
                "units": r["units"],
                "seasonal_adjustment": r["seasonal_adjustment"],
            }
            for o in obs
            if o["realtime_end"] == REALTIME_MAX
        ]
        return rows, floor, fell_back

    def _observations(self, sid: str, floor: date | None) -> list[dict[str, Any]]:
        params = {
            "series_id": sid,
            "realtime_start": floor.isoformat() if floor else REALTIME_MIN,
            "realtime_end": REALTIME_MAX,
        }
        return list(
            self._paged(
                "series/observations", params, "observations", self.fred["page_limit_observations"]
            )
        )

    # --- update --------------------------------------------------------------------------------

    def update(self) -> dict[str, Any]:
        """Actualización semanal: series/updates si es posible; crawl completo si no."""
        cursors = self._load_cursors()
        now = self._now()
        last_ok = _parse_cursor(cursors.get("last_update_ok"))
        last_crawl = _parse_cursor(cursors.get("last_crawl_ok"))
        refresh = timedelta(days=self.fred["catalog_refresh_days"])
        fallback = timedelta(days=self.fred["updates_fallback_days"])

        stats: dict[str, Any] = {}
        if last_crawl is None or now - last_crawl > refresh or last_ok is None:
            stats["mode"] = "crawl"
            stats["crawl"] = self.crawl()
        elif now - last_ok > fallback:
            # series/updates solo cubre las dos últimas semanas: no sirve para cerrar el hueco.
            stats["mode"] = "crawl_fallback"
            stats["crawl"] = self.crawl()
        else:
            stats["mode"] = "updates"
            infos = list(
                self._paged(
                    "series/updates",
                    {"filter_value": "all"},
                    "seriess",
                    self.fred["page_limit_series"],
                )
            )
            ids = [i["id"] for i in infos]
            prev_rows = (
                read_table(
                    FRED_CATALOG,
                    self.paths.catalog,
                    source=SOURCE,
                    where="list_contains(?, series_id)",
                    params=[ids],
                ).to_pylist()
                if ids
                else []
            )
            known = {r["series_id"]: r for r in prev_rows}
            rows = []
            for info in infos:
                prev = known.get(info["id"])
                cats = set(prev["category_ids"] or []) if prev else set()
                paths = set(prev["category_path"].split(";")) if prev else set()
                rows.append(catalog_row(info, cats, paths))
            self._write_catalog(rows)
            stats["updated_series"] = len(rows)
            stats["new_series_without_category"] = sum(info["id"] not in known for info in infos)
        stats["observations"] = self.observations()
        cursors = self._load_cursors()
        cursors["last_update_ok"] = now.isoformat()
        self._dump_cursors(cursors)
        return stats

    # --- utilidades ----------------------------------------------------------------------------

    def pick_category(self, seed: int) -> dict[str, Any]:
        """Categoría al azar (semilla fija) entre las que tienen series elegibles."""
        catalog = read_table(FRED_CATALOG, self.paths.catalog, source=SOURCE).to_pylist()
        eligible, _ = eligibility(catalog, self.cfg, self._now().date())
        counts: Counter[int] = Counter()
        for r in eligible:
            counts.update(r["category_ids"] or [])
        if not counts:
            raise RuntimeError("no hay categorías con series elegibles; corre crawl primero")
        cid = random.Random(seed).choice(sorted(counts))
        return {"category_id": cid, "eligible_series": counts[cid]}

    def _get(
        self, endpoint: str, params: dict[str, Any], *, use_cache: bool = False
    ) -> dict[str, Any]:
        return self.client.get_json(endpoint, {**params, "file_type": "json"}, use_cache=use_cache)

    def _paged(
        self,
        endpoint: str,
        params: dict[str, Any],
        items_key: str,
        limit: int,
        *,
        use_cache: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Itera todas las páginas; falla si la API devuelve menos filas que `count`."""
        offset = 0
        while True:
            data = self._get(
                endpoint, {**params, "limit": limit, "offset": offset}, use_cache=use_cache
            )
            items = data[items_key]
            yield from items
            offset += len(items)
            total = int(data["count"])
            if offset >= total:
                return
            if not items:
                raise ApiError(
                    f"{endpoint}: paginación incompleta ({offset} de {total} filas)", status=None
                )

    def _write_catalog(self, rows: list[dict[str, Any]]) -> None:
        n = self.fred["n_buckets"]
        catalog = pa.Table.from_pylist(rows, schema=FRED_CATALOG.schema)
        upsert(catalog, FRED_CATALOG, self.paths.catalog, source=SOURCE, n_buckets=n)
        upsert(
            to_series_meta(catalog), SERIES_META, self.paths.series_meta, source=SOURCE, n_buckets=n
        )

    def _save_cursor(self, name: str) -> None:
        cursors = self._load_cursors()
        cursors[name] = self._now().isoformat()
        self._dump_cursors(cursors)

    def _load_cursors(self) -> dict[str, Any]:
        path = self.paths.cursors
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _dump_cursors(self, cursors: dict[str, Any]) -> None:
        path = self.paths.cursors
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cursors, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


# --- funciones puras -----------------------------------------------------------------------------


def catalog_row(info: dict[str, Any], category_ids: set[int], paths: set[str]) -> dict[str, Any]:
    """Fila de fred_catalog a partir del objeto `series` de la API."""
    title = info.get("title") or ""
    notes = info.get("notes")
    return {
        "source": SOURCE,
        "series_id": info["id"],
        "title": title,
        "frequency_short": info.get("frequency_short"),
        "units": info.get("units"),
        "seasonal_adjustment": info.get("seasonal_adjustment_short"),
        "observation_start": _date_or_none(info.get("observation_start")),
        "observation_end": _date_or_none(info.get("observation_end")),
        "last_updated": parse_last_updated(info.get("last_updated")),
        "category_ids": sorted(category_ids),
        "category_path": ";".join(sorted(p for p in paths if p)),
        "notes_present": notes is not None,
        "discontinued": "(DISCONTINUED)" in title.upper(),
        "copyrighted": "copyright" in (notes or "").lower(),
    }


def to_series_meta(catalog: pa.Table) -> pa.Table:
    """Proyección de fred_catalog al esquema común series_meta."""
    return pa.table(
        {
            "source": catalog["source"],
            "series_id": catalog["series_id"],
            "title": catalog["title"],
            "category_path": catalog["category_path"],
            "start_date": catalog["observation_start"],
            "end_date": catalog["observation_end"],
            "last_updated": catalog["last_updated"],
            "discontinued": catalog["discontinued"],
            "copyrighted": catalog["copyrighted"],
        },
        schema=SERIES_META.schema,
    )


def eligibility(
    catalog: list[dict[str, Any]], cfg: dict[str, Any], today: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filtro objetivo de elegibilidad (config.yaml). Devuelve las elegibles y los conteos."""
    min_obs = cfg["eligibility"]["min_obs"]
    max_stale = cfg["eligibility"]["max_staleness_days"]
    counts: dict[str, Any] = {
        "total": len(catalog),
        "discontinued": 0,
        "unsupported_frequency": Counter(),
        "missing_dates": 0,
        "too_short": 0,
        "stale": 0,
        "eligible": 0,
    }
    eligible = []
    for r in catalog:
        freq = FREQ_MAP.get(r["frequency_short"] or "")
        start, end = r["observation_start"], r["observation_end"]
        if r["discontinued"]:
            counts["discontinued"] += 1
        elif freq is None:
            counts["unsupported_frequency"][r["frequency_short"]] += 1
        elif start is None or end is None:
            counts["missing_dates"] += 1
        elif (end - start).days / PERIOD_DAYS[freq] + 1 < min_obs[freq]:
            counts["too_short"] += 1
        elif (today - end).days > max_stale[freq]:
            counts["stale"] += 1
        else:
            eligible.append(r)
    counts["eligible"] = len(eligible)
    counts["unsupported_frequency"] = dict(counts["unsupported_frequency"])
    return eligible, counts


def parse_last_updated(value: str | None) -> datetime | None:
    """'2013-07-31 09:26:16-05' -> datetime UTC."""
    if not value:
        return None
    value = re.sub(r"([+-]\d{2})$", r"\g<1>00", value.strip())
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S%z").astimezone(UTC)


def _needs_fetch(r: dict[str, Any], st: dict[str, Any] | None) -> bool:
    return st is None or st["last_updated"] != r["last_updated"]


def _date_or_none(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _parse_cursor(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


# --- CLI -----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.ingest.fred")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("crawl", help="recorre el árbol de categorías completo")
    obs = sub.add_parser("observations", help="descarga observaciones de series elegibles")
    obs.add_argument("--category", type=int, default=None)
    obs.add_argument("--max-series", type=int, default=None)
    sub.add_parser("update", help="actualización incremental semanal")
    pick = sub.add_parser("pick-category", help="categoría al azar para verificación")
    pick.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    load_dotenv(REPO_ROOT / ".env")
    cfg = load_config()
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        print("FRED_API_KEY no está definida (ver .env.example)", file=sys.stderr)
        return 2
    logger = get_logger("ingest.fred", cfg["logging"]["level"])
    ingestor = FredIngestor.from_config(cfg, api_key, logger)
    try:
        if args.cmd == "pick-category":
            seed = args.seed if args.seed is not None else cfg["fred"]["verification_seed"]
            print(json.dumps(ingestor.pick_category(seed)))
            return 0
        with run([SOURCE], _resolve(cfg["logging"]["runs_file"])) as record:
            with record.stage(args.cmd):
                if args.cmd == "crawl":
                    stats: dict[str, Any] = ingestor.crawl()
                elif args.cmd == "observations":
                    stats = ingestor.observations(args.category, args.max_series)
                else:
                    stats = ingestor.update()
            record.n_series[SOURCE] = _n_series(stats)
            record.details = {
                "command": args.cmd,
                "stats": stats,
                "http_requests": ingestor.client.n_requests,
                "http_retries": ingestor.client.n_retries,
            }
        print(json.dumps(stats, default=str, indent=2))
    finally:
        ingestor.client.close()
    return 0


def _n_series(stats: dict[str, Any]) -> int:
    if "observations" in stats:
        return stats["observations"]["fetched"]
    return stats.get("fetched", stats.get("series", 0))


if __name__ == "__main__":
    sys.exit(main())
