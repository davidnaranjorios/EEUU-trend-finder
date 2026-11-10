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
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
from dotenv import load_dotenv

from src.config import REPO_ROOT, in_young_scope, load_config
from src.ingest.http import ApiClient, ApiError, RateLimiter
from src.ingest.store import UpsertResult, read_table, upsert
from src.log import get_logger, run
from src.schemas import ELIGIBILITY, SERIES, SERIES_META, VINTAGE_LINEAGE, TableSpec

SOURCE = "fred"
BASE_URL = "https://api.stlouisfed.org/fred/"
BASE_URL_V2 = "https://api.stlouisfed.org/fred/v2/"
# Código de salida cuando una corrida acotada (--max-*) deja trabajo pendiente.
EXIT_PENDING = 10
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

# Atributos que no trae category/series: tipo geográfico (tags del grupo geot), si la serie es
# de EE. UU., releases y agencias. Los nombres de tags salen de la API, no del código.
SERIES_ATTRS = TableSpec(
    name="fred_series_attrs",
    schema=pa.schema(
        [
            pa.field("source", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=False),
            pa.field("geo_types", pa.list_(pa.string())),
            pa.field("is_usa", pa.bool_(), nullable=False),
            pa.field("release_ids", pa.list_(pa.int64())),
            pa.field("agencies", pa.list_(pa.string())),
            pa.field("popularity", pa.int64()),
        ]
    ),
    key=("source", "series_id"),
)
GEO_TAG_GROUP = "geot"
USA_TAG = "usa"

# Muestra estratificada para vintages de ALFRED: se sortea una vez y queda fija.
VINTAGE_SAMPLE = TableSpec(
    name="fred_vintage_sample",
    schema=pa.schema(
        [
            pa.field("source", pa.string(), nullable=False),
            pa.field("series_id", pa.string(), nullable=False),
            pa.field("stratum", pa.string(), nullable=False),
            pa.field("seed", pa.int64(), nullable=False),
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
    def series_attrs(self) -> Path:
        return self.processed / "_internal" / "fred_series_attrs"

    @property
    def fetch_state(self) -> Path:
        return self.processed / "ingest_state" / "fred_fetch"

    @property
    def vintage_state(self) -> Path:
        return self.processed / "ingest_state" / "fred_vintages"

    @property
    def vintage_sample(self) -> Path:
        return self.processed / "_internal" / "fred_vintage_sample"

    @property
    def eligibility(self) -> Path:
        return self.processed / "lineage" / "eligibility"

    @property
    def vintage_lineage(self) -> Path:
        return self.processed / "lineage" / "vintages"

    @property
    def cursors(self) -> Path:
        return self.processed / "ingest_state" / "fred_cursors.json"


def dedupe_updates(infos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Una entrada por serie de `series/updates`, la de `last_updated` más reciente.

    Mantiene el orden de la primera aparición de cada id.
    """
    best: dict[str, dict[str, Any]] = {}
    for info in infos:
        prev = best.get(info["id"])
        if prev is None or str(info.get("last_updated", "")) > str(prev.get("last_updated", "")):
            best[info["id"]] = info
    return list(best.values())


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
        v2: ApiClient | None = None,
    ) -> None:
        self.client = client
        self.v2 = v2
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
        paths = paths_from_config(cfg)
        # v1 y v2 comparten el rate limit: es un solo límite por API key.
        limiter = RateLimiter(fred["requests_per_minute"])
        common: dict[str, Any] = {
            "requests_per_minute": fred["requests_per_minute"],
            "max_retries": fred["max_retries"],
            "timeout_s": fred["timeout_s"],
            "backoff_base_s": fred["backoff_base_s"],
            "limiter": limiter,
            "logger": logger,
        }
        client = ApiClient(BASE_URL, {"api_key": api_key}, raw_dir=paths.raw / SOURCE, **common)
        # v2 autentica por header. Sin copia raw: cada página pesa ~21 MB de JSON.
        v2 = ApiClient(BASE_URL_V2, {}, headers={"Authorization": f"Bearer {api_key}"}, **common)
        return cls(client, cfg, paths, logger=logger, v2=v2)

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

    # --- crawl-meta ----------------------------------------------------------------------------

    def crawl_meta(self) -> dict[str, Any]:
        """Tipo geográfico, EE. UU./no, releases y agencias de cada serie (solo metadatos)."""
        limit = self.fred["page_limit_series"]
        geo: dict[str, set[str]] = defaultdict(set)
        popularity: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        geo_tags = [
            t["name"] for t in self._paged("tags", {"tag_group_id": GEO_TAG_GROUP}, "tags", limit)
        ]
        for tag in [*geo_tags, USA_TAG]:
            n = 0
            # tags/series devuelve un `count` inestable: se pagina hasta una página corta.
            for info in self._paged(
                "tags/series",
                {"tag_names": tag},
                "seriess",
                limit,
                use_cache=True,
                trust_count=False,
            ):
                geo[info["id"]].add(tag)
                popularity[info["id"]] = info.get("popularity")
                n += 1
            tag_counts[tag] = n

        releases: dict[str, set[int]] = defaultdict(set)
        agencies: dict[str, set[str]] = defaultdict(set)
        all_releases = list(self._paged("releases", {}, "releases", limit))
        for rel in all_releases:
            rid = rel["id"]
            names = [
                s["name"]
                for s in self._get("release/sources", {"release_id": rid}, use_cache=True)[
                    "sources"
                ]
            ]
            for info in self._paged(
                "release/series", {"release_id": rid}, "seriess", limit, use_cache=True
            ):
                releases[info["id"]].add(rid)
                agencies[info["id"]].update(names)
                popularity.setdefault(info["id"], info.get("popularity"))

        rows = []
        for sid in sorted(geo.keys() | releases.keys()):
            tags = geo.get(sid, set())
            rows.append(
                {
                    "source": SOURCE,
                    "series_id": sid,
                    "geo_types": sorted(tags - {USA_TAG}),
                    "is_usa": USA_TAG in tags,
                    "release_ids": sorted(releases.get(sid, set())),
                    "agencies": sorted(agencies.get(sid, set())),
                    "popularity": popularity.get(sid),
                }
            )
        upsert(
            pa.Table.from_pylist(rows, schema=SERIES_ATTRS.schema),
            SERIES_ATTRS,
            self.paths.series_attrs,
            source=SOURCE,
            n_buckets=self.fred["n_buckets"],
        )
        self._save_cursor("last_crawl_meta_ok")
        stats = {
            "series": len(rows),
            "geo_tags": tag_counts,
            "releases": len(all_releases),
            "series_with_release": sum(bool(r["release_ids"]) for r in rows),
        }
        self.log.info("crawl-meta terminado", extra={"fields": stats})
        return stats

    # --- observations --------------------------------------------------------------------------

    def observations(
        self,
        category_id: int | None = None,
        max_series: int | None = None,
        ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Valores vigentes (v1) de las series objetivo nuevas o actualizadas (reanudable).

        Objetivo: los grupos `download.groups` de la última corrida del embudo, o `ids`.
        `vintage_date` queda NULL ("no aplica"): las revisiones reemplazan el valor. El
        historial de publicación solo se guarda para la muestra de `vintages`.
        """
        targets = self._targets(
            ids,
            self.cfg["download"]["groups"],
            category_id,
            with_reference=category_id is None,
            young=True,
        )
        state = self._state(self.paths.fetch_state)
        todo = [r for r in targets if _needs_fetch(r, state.get(r["series_id"]))]
        if max_series is not None:
            todo = todo[:max_series]
        tally = self._fetch_batches(todo, self.paths.fetch_state, self._fetch_current)
        stats: dict[str, Any] = {"targets": len(targets), "to_fetch": len(todo), **tally}
        stats["pending"] = _pending(targets, state, tally)
        self.log.info("observaciones terminadas", extra={"fields": stats})
        return stats

    def _fetch_current(self, r: dict[str, Any]) -> list[dict[str, Any]]:
        obs = self._paged(
            "series/observations",
            {"series_id": r["series_id"]},
            "observations",
            self.fred["page_limit_observations"],
        )
        return [_series_row(r, o["date"], o["value"], None) for o in obs]

    # --- download (API v2 por release) ---------------------------------------------------------

    def download(self, max_releases: int | None = None) -> dict[str, Any]:
        """Valores vigentes de los grupos `download.groups` vía v2, un release por tramo.

        Cada release terminado queda en el cursor `v2_releases_done` con su fecha: una corrida
        cortada se reanuda en el primer release pendiente (las filas ya escritas no se
        duplican). Solo se escriben las series objetivo; el resto del release se descarta.
        """
        if self.v2 is None:
            raise RuntimeError("download requiere el cliente de la API v2")
        targets = {
            r["series_id"]: r
            for r in self._targets(
                None, self.cfg["download"]["groups"], with_reference=True, young=True
            )
        }
        attrs = {
            r["series_id"]: r
            for r in read_table(SERIES_ATTRS, self.paths.series_attrs, source=SOURCE).to_pylist()
            if r["series_id"] in targets
        }
        releases = sorted({rid for a in attrs.values() for rid in a["release_ids"] or []})
        done = self._load_cursors().get("v2_releases_done", {})
        pending = [rid for rid in releases if str(rid) not in done]
        batch = list(pending if max_releases is None else pending[:max_releases])

        tally: Counter[str] = Counter()
        for rid in batch:
            res = self._download_release(rid, targets)
            tally.update(res)
            cursors = self._load_cursors()
            cursors.setdefault("v2_releases_done", {})[str(rid)] = self._now().isoformat()
            self._dump_cursors(cursors)
            self.log.info(
                "release descargado",
                extra={"fields": {"release_id": rid, **res, "pending": len(pending) - 1}},
            )
            pending.remove(rid)
        stats: dict[str, Any] = {
            "targets": len(targets),
            "targets_without_release": len(targets) - len(attrs),
            "releases": len(releases),
            "releases_done_now": len(batch),
            "pending_releases": len(pending),
            **tally,
        }
        self.log.info("download terminado", extra={"fields": stats})
        return stats

    def _download_release(self, rid: int, targets: dict[str, dict[str, Any]]) -> Counter[str]:
        tally: Counter[str] = Counter()
        params: dict[str, Any] = {
            "release_id": rid,
            "format": "json",
            "limit": self.cfg["download"]["v2_page_limit"],
        }
        while True:
            assert self.v2 is not None
            data = self.v2.get_json("release/observations", params)
            rows: list[dict[str, Any]] = []
            seen: dict[str, int] = {}
            for s in data["series"]:
                tally["series_in_pages"] += 1
                r = targets.get(s["series_id"])
                if r is None:
                    continue
                rows.extend(_series_row(r, o["date"], o["value"], None) for o in s["observations"])
                seen[s["series_id"]] = seen.get(s["series_id"], 0) + len(s["observations"])
            self._write_rows(rows, [targets[sid] for sid in seen], seen, self.paths.fetch_state)
            tally["rows_written"] += len(rows)
            tally["pages"] += 1
            if str(data.get("has_more")).lower() != "true":
                return tally
            params["next_cursor"] = data["next_cursor"]

    # --- vintages (ALFRED, solo una muestra) ---------------------------------------------------

    def vintages(
        self,
        ids: list[str] | None = None,
        max_series: int | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Historial completo de publicación (ALFRED) para la muestra estratificada + `ids`.

        La muestra se sortea una vez (semilla de config) y queda guardada en
        `fred_vintage_sample`; las corridas siguientes la reutilizan. `ids` permite añadir
        series desde fuera de src/ (p. ej., las que eval/ necesite) sin que src/ lea eval/.

        Cada serie intentada deja su resultado en `lineage/vintages` (`run_id`): `ok`,
        `no_existe_en_alfred` (FRED la tiene, ALFRED no; no se reintenta mientras no cambie
        `last_updated`) o `error_api` (se reintenta en la próxima corrida).
        """
        sample = self._vintage_sample()
        wanted = sorted(set(sample) | set(ids or []))
        catalog = {
            r["series_id"]: r
            for r in read_table(
                FRED_CATALOG,
                self.paths.catalog,
                source=SOURCE,
                where="list_contains(?, series_id)",
                params=[wanted],
            ).to_pylist()
            if FREQ_MAP.get(r["frequency_short"] or "")
        }
        state = self._state(self.paths.vintage_state)
        targets = [catalog[s] for s in wanted if s in catalog]
        todo = [r for r in targets if _needs_fetch(r, state.get(r["series_id"]))]
        if max_series is not None:
            todo = todo[:max_series]
        outcomes: dict[str, tuple[str, int, str | None]] = {}
        tally = self._fetch_batches(
            todo,
            self.paths.vintage_state,
            self._fetch_vintages,
            outcomes=outcomes,
            permanent=_alfred_missing,
        )
        self._write_vintage_lineage(outcomes, run_id or uuid.uuid4().hex)
        stats: dict[str, Any] = {
            "sample": len(sample),
            "extra_ids": len(set(ids or []) - set(sample)),
            "unknown_ids": len(set(wanted) - set(catalog)),
            "to_fetch": len(todo),
            **tally,
        }
        stats["pending"] = _pending(targets, state, tally)
        self.log.info("vintages terminadas", extra={"fields": stats})
        return stats

    def _write_vintage_lineage(
        self, outcomes: dict[str, tuple[str, int, str | None]], run_id: str
    ) -> None:
        if not outcomes:
            return
        now = self._now().replace(microsecond=0)
        rows = [
            {
                "run_id": run_id,
                "source": SOURCE,
                "series_id": sid,
                "status": status,
                "n_rows": n_rows,
                "detail": detail,
                "decided_at": now,
            }
            for sid, (status, n_rows, detail) in sorted(outcomes.items())
        ]
        upsert(
            pa.Table.from_pylist(rows, schema=VINTAGE_LINEAGE.schema),
            VINTAGE_LINEAGE,
            self.paths.vintage_lineage,
            source=SOURCE,
            n_buckets=self.fred["n_buckets"],
        )

    def _vintage_sample(self) -> list[str]:
        stored = read_table(VINTAGE_SAMPLE, self.paths.vintage_sample, source=SOURCE)
        if stored.num_rows:
            return sorted(stored.column("series_id").to_pylist())
        vcfg = self.cfg["vintages"]
        targets = self._targets(None, vcfg["groups"])
        attrs = {
            r["series_id"]: r
            for r in read_table(SERIES_ATTRS, self.paths.series_attrs, source=SOURCE).to_pylist()
        }
        strata: dict[str, list[str]] = defaultdict(list)
        for r in targets:
            agencies = (attrs.get(r["series_id"]) or {}).get("agencies") or []
            agency = agencies[0] if agencies else "sin_agencia"
            strata[f"{FREQ_MAP[r['frequency_short']]}|{agency}"].append(r["series_id"])
        picked = stratified_sample(strata, vcfg["sample_size"], vcfg["seed"])
        rows = [
            {"source": SOURCE, "series_id": sid, "stratum": stratum, "seed": vcfg["seed"]}
            for stratum, sids in picked.items()
            for sid in sids
        ]
        upsert(
            pa.Table.from_pylist(rows, schema=VINTAGE_SAMPLE.schema),
            VINTAGE_SAMPLE,
            self.paths.vintage_sample,
            source=SOURCE,
            n_buckets=self.fred["n_buckets"],
        )
        self.log.info(
            "muestra de vintages sorteada",
            extra={"fields": {"series": len(rows), "strata": len(picked)}},
        )
        return sorted(r["series_id"] for r in rows)

    def _fetch_vintages(self, r: dict[str, Any]) -> list[dict[str, Any]]:
        """Todas las filas realtime, en ventanas de a lo sumo N vintages por consulta.

        FRED puede contar una fecha más de las que da `series/vintagedates` para la misma
        ventana (DBAA y FIN14AAVOL: "There are 2001 vintage dates" en ventanas de 2.000). Si
        rechaza una ventana por el tope, se parte en dos y se reintenta.
        """
        sid = r["series_id"]
        dates = list(self._paged("series/vintagedates", {"series_id": sid}, "vintage_dates", 10000))
        cap = self.cfg["vintages"]["max_vintage_dates_per_request"]
        # Ventanas como rangos [i, j) de índices en `dates`.
        pending = deque((i, min(i + cap, len(dates))) for i in range(0, max(len(dates), 1), cap))
        obs: list[dict[str, Any]] = []
        while pending:
            i, j = pending.popleft()
            start = REALTIME_MIN if i == 0 else dates[i]
            end = REALTIME_MAX if j >= len(dates) else _minus_one_day(dates[j])
            try:
                obs.extend(
                    self._paged(
                        "series/observations",
                        {"series_id": sid, "realtime_start": start, "realtime_end": end},
                        "observations",
                        self.fred["page_limit_observations"],
                    )
                )
            except ApiError as exc:
                if not (_over_vintage_cap(exc) and j - i > 1):
                    raise
                mid = (i + j) // 2
                self.log.info(
                    "ventana de vintages partida",
                    extra={"fields": {"series_id": sid, "window": [start, end]}},
                )
                pending.extendleft([(mid, j), (i, mid)])
        return [
            _series_row(r, o["date"], o["value"], date.fromisoformat(o["realtime_start"]))
            for o in collapse_vintages(obs)
        ]

    # --- utilidades de descarga ----------------------------------------------------------------

    def _targets(
        self,
        ids: list[str] | None,
        groups: list[str],
        category_id: int | None = None,
        *,
        with_reference: bool = False,
        young: bool = False,
    ) -> list[dict[str, Any]]:
        """Filas de catálogo de las series objetivo, ordenadas por id.

        `young`: suma las series jóvenes (`eligibility.young`, ADR 0017) de nivel geográfico
        descargable; solo para descargar valores, nunca para vintages.

        `with_reference`: suma las series de referencia metodológica (context.reference_series,
        ADR 0009) aunque el embudo no las clasifique en `groups`: la ficha del fenómeno las
        necesita (deflactar, per cápita, recesiones). Nunca entran a detectores ni ranking.
        """
        if ids is None:
            lineage = read_table(ELIGIBILITY, self.paths.eligibility, source=SOURCE)
            if lineage.num_rows == 0:
                raise RuntimeError(
                    "no hay corridas del embudo; corre `python -m src.eligibility run`"
                )
            rows = lineage.to_pylist()
            last = max(rows, key=lambda r: (r["decided_at"], r["run_id"]))["run_id"]
            ids = [
                r["series_id"]
                for r in rows
                if r["run_id"] == last
                and (
                    r["group_name"] in groups
                    or (young and in_young_scope(r["group_name"], r.get("geo_level"), self.cfg))
                )
            ]
            if with_reference:
                refs = (self.cfg.get("context") or {}).get("reference_series") or {}
                ids += list(refs.values())
        catalog = read_table(
            FRED_CATALOG,
            self.paths.catalog,
            source=SOURCE,
            where="list_contains(?, series_id)",
            params=[sorted(set(ids))],
        ).to_pylist()
        out = [r for r in catalog if FREQ_MAP.get(r["frequency_short"] or "")]
        if category_id is not None:
            out = [r for r in out if category_id in (r["category_ids"] or [])]
        return sorted(out, key=lambda r: r["series_id"])

    def _state(self, path: Path) -> dict[str, dict[str, Any]]:
        return {r["series_id"]: r for r in read_table(FETCH_STATE, path, source=SOURCE).to_pylist()}

    def _fetch_batches(
        self,
        todo: list[dict[str, Any]],
        state_path: Path,
        fetch: Callable[[dict[str, Any]], list[dict[str, Any]]],
        *,
        outcomes: dict[str, tuple[str, int, str | None]] | None = None,
        permanent: Callable[[ApiError], str | None] | None = None,
    ) -> Counter[str]:
        """Baja `todo` por lotes. `permanent(exc)` da el motivo de un error que no se arregla
        reintentando: la serie queda en el estado con 0 filas (no se vuelve a pedir mientras no
        cambie `last_updated`) y cuenta en `no_data`. `outcomes` recibe (status, filas, motivo).
        """
        tally: Counter[str] = Counter({"fetched": 0, "series_errors": 0, "no_data": 0})
        batch_size = self.fred["obs_batch_size"]
        for i in range(0, len(todo), batch_size):
            rows: list[dict[str, Any]] = []
            fetched: list[dict[str, Any]] = []
            n_rows: dict[str, int] = {}
            ok = 0
            for r in todo[i : i + batch_size]:
                sid = r["series_id"]
                try:
                    got = fetch(r)
                except ApiError as exc:
                    if exc.status is None:  # reintentos agotados: abortar la corrida
                        raise
                    reason = permanent(exc) if permanent else None
                    if outcomes is not None:
                        outcomes[sid] = (reason or "error_api", 0, str(exc))
                    if reason:
                        tally["no_data"] += 1
                        fetched.append(r)
                        n_rows[sid] = 0
                        self.log.info(
                            "serie sin datos en la API",
                            extra={"fields": {"series_id": sid, "reason": reason}},
                        )
                        continue
                    tally["series_errors"] += 1
                    self.log.warning(
                        "serie omitida por error de la API",
                        extra={"fields": {"series_id": sid, "error": str(exc)}},
                    )
                    continue
                rows.extend(got)
                fetched.append(r)
                n_rows[sid] = len(got)
                ok += 1
                if outcomes is not None:
                    outcomes[sid] = ("ok", len(got), None)
            res = self._write_rows(rows, fetched, n_rows, state_path)
            tally["fetched"] += ok
            tally["rows_inserted"] += res.inserted
            tally["rows_unchanged"] += res.unchanged
            tally["rows_replaced"] += res.replaced
            self.log.info(
                "lote de observaciones",
                extra={"fields": {"done": tally["fetched"], "of": len(todo)}},
            )
        return tally

    def _write_rows(
        self,
        rows: list[dict[str, Any]],
        series: list[dict[str, Any]],
        n_rows: dict[str, int],
        state_path: Path,
    ) -> UpsertResult:
        # Primero los datos y después el estado: si algo se corta, se vuelve a pedir.
        res = upsert(
            pa.Table.from_pylist(rows, schema=SERIES.schema),
            SERIES,
            self.paths.series,
            source=SOURCE,
            n_buckets=self.fred["n_buckets"],
        )
        now = self._now().replace(microsecond=0)
        state_rows = [
            {
                "source": SOURCE,
                "series_id": r["series_id"],
                "last_updated": r["last_updated"],
                "fetched_at": now,
                "n_rows": n_rows[r["series_id"]],
                "realtime_floor": None,
            }
            for r in series
        ]
        upsert(
            pa.Table.from_pylist(state_rows, schema=FETCH_STATE.schema),
            FETCH_STATE,
            state_path,
            source=SOURCE,
            n_buckets=self.fred["n_buckets"],
        )
        return res

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
            # series/updates pagina por last_updated: si FRED actualiza series mientras se
            # recorre, una serie puede aparecer en dos páginas. Se conserva la versión más nueva.
            infos = dedupe_updates(infos)
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
        """Categoría al azar (semilla fija) entre las que tienen series objetivo."""
        counts: Counter[int] = Counter()
        for r in self._targets(None, self.cfg["download"]["groups"]):
            counts.update(r["category_ids"] or [])
        if not counts:
            raise RuntimeError("no hay categorías con series objetivo; corre el embudo primero")
        cid = random.Random(seed).choice(sorted(counts))
        return {"category_id": cid, "eligible_series": counts[cid]}

    def is_stale(self, cmd: str) -> bool:
        """¿La última corrida exitosa de `crawl`/`crawl-meta` es más vieja que el umbral?"""
        cursor = {"crawl": "last_crawl_ok", "crawl-meta": "last_crawl_meta_ok"}[cmd]
        last = _parse_cursor(self._load_cursors().get(cursor))
        return last is None or self._now() - last > timedelta(
            days=self.fred["catalog_refresh_days"]
        )

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
        trust_count: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """Itera todas las páginas; falla si la API devuelve menos filas que `count`.

        Con `trust_count=False` (endpoints con `count` inestable) termina en la primera página
        con menos de `limit` filas.
        """
        offset = 0
        while True:
            data = self._get(
                endpoint, {**params, "limit": limit, "offset": offset}, use_cache=use_cache
            )
            items = data[items_key]
            yield from items
            offset += len(items)
            if not trust_count:
                if len(items) < limit:
                    return
                continue
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


def parse_last_updated(value: str | None) -> datetime | None:
    """'2013-07-31 09:26:16-05' -> datetime UTC."""
    if not value:
        return None
    value = re.sub(r"([+-]\d{2})$", r"\g<1>00", value.strip())
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S%z").astimezone(UTC)


def _series_row(
    r: dict[str, Any], obs_date: str, value: str, vintage_date: date | None
) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "series_id": r["series_id"],
        "date": date.fromisoformat(obs_date),
        "value": None if value == "." else float(value),
        "vintage_date": vintage_date,
        "frequency": FREQ_MAP[r["frequency_short"]],
        "units": r["units"],
        "seasonal_adjustment": r["seasonal_adjustment"],
    }


def collapse_vintages(obs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Una fila por (fecha, publicación de un valor distinto).

    Al pedir el rango realtime en tramos, una fila vigente a través del borde aparece en los
    dos tramos con el mismo valor; se conserva solo la primera (su fecha real de publicación).
    """
    out: list[dict[str, Any]] = []
    last: dict[str, str] = {}
    for o in sorted(obs, key=lambda o: (o["date"], o["realtime_start"])):
        if last.get(o["date"]) == o["value"]:
            continue
        last[o["date"]] = o["value"]
        out.append(o)
    return out


def stratified_sample(strata: dict[str, list[str]], size: int, seed: int) -> dict[str, list[str]]:
    """Muestreo aleatorio estratificado proporcional, con al menos una serie por estrato.

    Determinista dada la semilla. Si hay más estratos que `size`, cada estrato aporta una.
    """
    total = sum(len(v) for v in strata.values())
    if total == 0 or size == 0:
        return {}
    rng = random.Random(seed)
    out = {}
    for name in sorted(strata):
        members = sorted(strata[name])
        k = min(len(members), max(1, round(size * len(members) / total)))
        out[name] = sorted(rng.sample(members, k))
    return out


def paths_from_config(cfg: dict[str, Any]) -> Paths:
    return Paths(_resolve(cfg["paths"]["processed_dir"]), _resolve(cfg["paths"]["raw_dir"]))


def _minus_one_day(iso: str) -> str:
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


def _pending(
    targets: list[dict[str, Any]], state: dict[str, dict[str, Any]], tally: Counter[str]
) -> int:
    """Series que quedan por bajar. Las que fallaron por error de la API no cuentan: se
    reintentan en la próxima corrida, pero no deben mantener vivo un bucle de tramos."""
    needed = sum(_needs_fetch(r, state.get(r["series_id"])) for r in targets)
    return needed - tally["fetched"] - tally["series_errors"] - tally["no_data"]


def _alfred_missing(exc: ApiError) -> str | None:
    """Motivo permanente: la serie existe en FRED pero no en ALFRED (sin historial)."""
    if exc.status == 400 and "does not exist in alfred" in str(exc).lower():
        return "no_existe_en_alfred"
    return None


def _over_vintage_cap(exc: ApiError) -> bool:
    return exc.status == 400 and "maximum number of vintage dates" in str(exc).lower()


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
    for name, help_ in (
        ("crawl", "recorre el árbol de categorías completo"),
        ("crawl-meta", "tipo geográfico, releases y agencias de cada serie"),
    ):
        c = sub.add_parser(name, help=help_)
        c.add_argument(
            "--if-stale",
            action="store_true",
            help="solo si la última corrida exitosa tiene más de fred.catalog_refresh_days",
        )
    obs = sub.add_parser("observations", help="valores vigentes (v1) de las series objetivo")
    obs.add_argument("--category", type=int, default=None)
    obs.add_argument("--max-series", type=int, default=None)
    obs.add_argument("--ids-file", type=Path, default=None)
    dl = sub.add_parser("download", help="valores vigentes vía API v2, un release por tramo")
    dl.add_argument("--max-releases", type=int, default=None)
    vin = sub.add_parser("vintages", help="historial ALFRED de la muestra estratificada")
    vin.add_argument("--max-series", type=int, default=None)
    vin.add_argument("--ids-file", type=Path, default=None, help="un id por línea")
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
    ids = _read_ids(args.ids_file) if getattr(args, "ids_file", None) else None
    try:
        if getattr(args, "if_stale", False) and not ingestor.is_stale(args.cmd):
            print(json.dumps({"skipped": args.cmd, "reason": "catálogo vigente"}))
            return 0
        if args.cmd == "pick-category":
            seed = args.seed if args.seed is not None else cfg["fred"]["verification_seed"]
            print(json.dumps(ingestor.pick_category(seed)))
            return 0
        with run([SOURCE], _resolve(cfg["logging"]["runs_file"])) as record:
            with record.stage(args.cmd):
                if args.cmd == "crawl":
                    stats: dict[str, Any] = ingestor.crawl()
                elif args.cmd == "crawl-meta":
                    stats = ingestor.crawl_meta()
                elif args.cmd == "observations":
                    stats = ingestor.observations(args.category, args.max_series, ids)
                elif args.cmd == "download":
                    stats = ingestor.download(args.max_releases)
                elif args.cmd == "vintages":
                    stats = ingestor.vintages(ids, args.max_series, record.run_id)
                else:
                    stats = ingestor.update()
            record.n_series[SOURCE] = _n_series(stats)
            record.details = {
                "command": args.cmd,
                "stats": stats,
                "http_requests": ingestor.client.n_requests
                + (ingestor.v2.n_requests if ingestor.v2 else 0),
                "http_retries": ingestor.client.n_retries
                + (ingestor.v2.n_retries if ingestor.v2 else 0),
            }
        print(json.dumps(stats, default=str, indent=2))
    finally:
        ingestor.client.close()
        if ingestor.v2 is not None:
            ingestor.v2.close()
    pending = stats.get("pending_releases", stats.get("pending", 0))
    return EXIT_PENDING if pending else 0


def _read_ids(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return sorted({line.strip() for line in lines if line.strip() and not line.startswith("#")})


def _n_series(stats: dict[str, Any]) -> int:
    if "observations" in stats:
        return stats["observations"]["fetched"]
    if "releases_done_now" in stats:
        return stats["targets"]
    return stats.get("fetched", stats.get("series", 0))


if __name__ == "__main__":
    sys.exit(main())
