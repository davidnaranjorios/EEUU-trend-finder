"""Adaptador Census (Fase 4): datasets timeseries de api.census.gov con el contrato común.

Unidad = dataset con `c_isTimeseries` en `data.json` (todos; sin listas en código).
`catalog_version` = campo `modified` del dataset.

Descubrimiento genérico por dataset (`variables.json` + `geography.json`):

- Se ignoran las variables `predicateOnly` (for, in, ucgid, time) y las de `predicateType`
  nulo (componentes de geografía).
- Variables "anotación": las nombradas en `attributes` de otra variable. Si son atributos de
  una dimensión (p. ej. `<DIM>_LABEL`) se piden como etiqueta; si lo son de una medida (flags,
  `<VAR>_F`) no se piden.
- Medidas: `predicateType` int/float sin `values` enumerados. Las que por su etiqueta son
  error estándar, coeficiente de variación o flag (`NON_SERIES_MEASURE_RE`) no son series y
  se cuentan. Una medida cuyos valores repiten el año o el mes de `time` es un componente de
  tiempo y se descarta.
- Dimensiones (categóricas): el resto (string, o con `values`). Sobre los datos se depuran:
  constantes y determinadas por `time` (año, mes, fecha de actualización...) salen de la
  clave; si una columna está determinada por otra más corta (código -> descripción), la
  larga queda como etiqueta de la corta. Una dimensión booleana cuya etiqueta menciona el
  ajuste estacional (`SEASONAL_LABEL_RE`) da SA/NSA; una booleana cuya etiqueta menciona
  "error" marca filas de error estándar, que no son series: se excluyen y se cuentan.
- Formato "largo" (sin medidas numéricas en metadatos, como EITS): la variable de valor es
  la columna string que es numérica en los datos y tiene más valores distintos.
- Geografía: si existe el nivel `us` se pide `for=us:*` (nacional). Si no, se pide solo el
  primer nivel de `geography.json` (el más agregado): internacional si su nombre habla de
  países o del mundo (`INTERNATIONAL_GEO_RE`), regional en otro caso; los demás niveles se
  omiten y se cuentan. Sin geografía declarada: sin_geografia.

Volumen: antes de la bajada completa se hace un sondeo (`time=from <año anterior>`) con un
cliente de pocos reintentos y timeout corto. Si falla (p. ej. la API corta la conexión por
tamaño), o si el número de series supera `max_series_per_unit`, o la bajada completa supera
`max_rows_per_unit`, o hacen falta más de `max_requests_per_unit` solicitudes, la unidad no se
recorre: `catalog_rows` no devuelve filas, se registra en el log y en
`ingest_state/census_skipped.json` con el motivo. Los fallos transitorios hacen que la unidad
se reintente en la próxima corrida (versión None).

`modified` no cambia con cada publicación (EITS: 2017), así que `values_version` es
`modified` + hash de los valores bajados.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from src.ingest import common
from src.ingest.common import SourcePaths, Unit
from src.ingest.http import ApiClient, ApiError, RateLimiter

SOURCE = "census"
BASE_URL = "https://api.census.gov"
CATALOG_ENDPOINT = "/data.json"
KEY_ENV = "CENSUS_API_KEY"
TIME_VAR = "time"
NATIONAL_LEVEL = "us"
START_PREDICATE = "from 1900"

NUMERIC_TYPES = frozenset({"int", "float"})
TRUE_VALUES = frozenset({"yes", "y", "true", "1"})
FALSE_VALUES = frozenset({"no", "n", "false", "0"})
NON_SERIES_MEASURE_RE = re.compile(
    r"\berror\b|coefficient of variation|margin of error|\bflag\b", re.IGNORECASE
)
# Dimensiones string que son anotaciones de otra variable (flags, notas, errores de medida).
ANNOTATION_LABEL_RE = re.compile(
    r"\bflag\b|standard error|coefficient of variation|margin of error|footnote", re.IGNORECASE
)
ERROR_LABEL_RE = re.compile(r"\berror\b", re.IGNORECASE)
SEASONAL_LABEL_RE = re.compile(r"season", re.IGNORECASE)
UNITS_LABEL_RE = re.compile(r"\bunits?\b|\b(item|data) type\b", re.IGNORECASE)
INTERNATIONAL_GEO_RE = re.compile(r"countr|world|international", re.IGNORECASE)
TIME_FORMATS = (
    (re.compile(r"^(\d{4})$"), "A", "YYYY"),
    (re.compile(r"^(\d{4})-(\d{2})$"), "M", "YYYY-MM"),
    (re.compile(r"^(\d{4})-Q([1-4])$"), "Q", "YYYY-Qn"),
)
# Fracción mínima de valores numéricos para tomar una columna string como variable de valor.
NUMERIC_SHARE = 0.95

# Valores por defecto de los parámetros propuestos para config.yaml (sources.census).
DEFAULTS: dict[str, Any] = dict(
    max_series_per_unit=200_000,
    max_rows_per_unit=3_000_000,
    max_requests_per_unit=20,
    max_get_vars=50,
    probe_timeout_s=120,
    probe_max_retries=1,
    cache_units=3,
)


@dataclass
class Plan:
    """Lo que se sabe de un dataset antes de pedir datos."""

    unit_id: str
    endpoint: str
    title: str
    modified: str | None
    measures: list[str] = field(default_factory=list)
    categoricals: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)  # dimensión -> variable etiqueta
    var_labels: dict[str, str] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    geo_for: str | None = None
    geo_level: str = "sin_geografia"
    geo_detail: str | None = None
    geo_levels_omitted: int = 0
    measures_excluded: int = 0
    annotations_excluded: int = 0
    skip_reason: str | None = None


@dataclass
class SeriesData:
    title: str
    units: str | None
    seasonal_adjustment: str | None
    formats: set[str] = field(default_factory=set)
    obs: dict[date, float | None] = field(default_factory=dict)


@dataclass
class Loaded:
    series: dict[str, SeriesData]
    counts: Counter[str]
    digest: str | None
    skip_reason: str | None = None
    transient: bool = False
    geo_level: str | None = None
    geo_detail: str | None = None


# --- funciones puras ------------------------------------------------------------------------


def parse_time(value: str) -> tuple[str | None, str, date | None]:
    """(frecuencia del esquema o None, formato, fecha) de un valor de `time`."""
    for rx, freq, fmt in TIME_FORMATS:
        m = rx.match(value or "")
        if not m:
            continue
        year = int(m.group(1))
        if freq == "A":
            return freq, fmt, date(year, 1, 1)
        if freq == "M":
            month = int(m.group(2))
            return (freq, fmt, date(year, month, 1)) if 1 <= month <= 12 else (None, fmt, None)
        return freq, fmt, date(year, 3 * int(m.group(2)) - 2, 1)
    return None, "desconocido", None


def parse_number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN -> None


def is_boolean(values: set[str]) -> bool:
    low = {str(v).strip().lower() for v in values if v not in (None, "")}
    return bool(low) and low <= TRUE_VALUES | FALSE_VALUES


def is_true(value: Any) -> bool:
    return str(value).strip().lower() in TRUE_VALUES


def dataset_path(unit_id: str) -> str:
    return unit_id.replace("/", ":")


def _labels_of(var: dict[str, Any]) -> list[str]:
    attrs = var.get("attributes") or ""
    return [a.strip() for a in attrs.split(",") if a.strip()]


def plan_dataset(
    unit_id: str,
    endpoint: str,
    title: str,
    modified: str | None,
    variables: dict[str, Any],
    geography: dict[str, Any],
) -> Plan:
    """Clasifica variables y geografía de un dataset (solo metadatos)."""
    plan = Plan(unit_id, endpoint, title, modified)
    vars_ = variables.get("variables", {})
    plan.var_labels = {k: str(v.get("label") or k) for k, v in vars_.items()}
    if TIME_VAR not in vars_:
        plan.skip_reason = "sin_variable_time"
    attribute_of: dict[str, str] = {}
    for name, var in vars_.items():
        for a in _labels_of(var):
            if a in vars_ and a != name:
                attribute_of.setdefault(a, name)
    for name, var in vars_.items():
        if var.get("predicateOnly") or var.get("predicateType") is None or name in attribute_of:
            continue
        ptype = var.get("predicateType")
        if ptype in NUMERIC_TYPES and not var.get("values"):
            if NON_SERIES_MEASURE_RE.search(plan.var_labels[name]):
                plan.measures_excluded += 1
            else:
                plan.measures.append(name)
        elif ANNOTATION_LABEL_RE.search(plan.var_labels[name]):
            plan.annotations_excluded += 1
        else:
            plan.categoricals.append(name)
        if str(var.get("required", "")).lower() == "true":
            plan.required.append(name)
    for a, owner in attribute_of.items():
        if owner in plan.categoricals and a not in plan.labels.values():
            plan.labels.setdefault(owner, a)
    plan.measures.sort()
    plan.categoricals.sort()

    levels = [str(f.get("name")) for f in geography.get("fips", []) if f.get("name")]
    if NATIONAL_LEVEL in levels:
        plan.geo_for, plan.geo_level, plan.geo_detail = f"{NATIONAL_LEVEL}:*", "nacional", "us"
        plan.geo_levels_omitted = len(levels) - 1
    elif levels:
        first = levels[0]
        plan.geo_for = f"{first}:*"
        plan.geo_level = "internacional" if INTERNATIONAL_GEO_RE.search(first) else "regional"
        plan.geo_detail = first
        plan.geo_levels_omitted = len(levels) - 1
    return plan


def get_chunks(plan: Plan, max_vars: int) -> list[list[str]]:
    """Listas `get=`: todas las dimensiones y etiquetas en cada una, medidas repartidas."""
    base = plan.categoricals + sorted(plan.labels.values())
    room = max_vars - len(base) - 1  # `time` también cuenta
    if room < 0 or (plan.measures and room == 0):
        return []
    if not plan.measures:
        return [base]
    return [base + plan.measures[i : i + room] for i in range(0, len(plan.measures), room)]


def _determines(rows: list[tuple], a: int, b: int) -> bool:
    seen: dict[Any, Any] = {}
    for r in rows:
        if seen.setdefault(r[a], r[b]) != r[b]:
            return False
    return True


@dataclass
class ChunkResult:
    series: dict[str, SeriesData]
    counts: Counter[str]


def parse_chunk(
    plan: Plan, header: list[str], rows: list[list[Any]], *, today_year: int | None = None
) -> ChunkResult:
    """Agrupa una respuesta GetData en series (ver docstring del módulo)."""
    counts: Counter[str] = Counter()
    idx = {h: i for i, h in enumerate(header)}
    t = idx[TIME_VAR]
    present_cats = [c for c in plan.categoricals if c in idx]
    label_cols = {c: plan.labels[c] for c in present_cats if plan.labels.get(c) in idx}
    geo_cols = [
        h for h in header if h not in plan.var_labels and h != TIME_VAR and h not in label_cols
    ]

    # Filas de error estándar: dimensión booleana con "error" en la etiqueta.
    error_cols = [
        c
        for c in present_cats
        if ERROR_LABEL_RE.search(plan.var_labels[c]) and is_boolean({r[idx[c]] for r in rows})
    ]
    if error_cols:
        kept = [r for r in rows if not any(is_true(r[idx[c]]) for c in error_cols)]
        counts["filas_error_excluidas"] += len(rows) - len(kept)
        rows = kept

    measures = [m for m in plan.measures if m in idx]
    candidates = [c for c in present_cats if c not in error_cols]
    if not plan.measures:
        best: tuple[int, str] | None = None
        for c in candidates:
            vals = [r[idx[c]] for r in rows if r[idx[c]] not in (None, "")]
            if not vals:
                continue
            numeric = sum(parse_number(v) is not None for v in vals)
            distinct = len(set(vals))
            if numeric >= NUMERIC_SHARE * len(vals) and distinct > 1:
                if best is None or distinct > best[0]:
                    best = (distinct, c)
        if best is None:
            return ChunkResult({}, Counter(sin_variable_de_valor=1))
        measures = [best[1]]
        candidates.remove(best[1])
    else:
        # Una medida que repite el año o el mes de `time` es un componente de tiempo.
        for m in list(measures):
            pairs = {(r[t], r[idx[m]]) for r in rows if r[idx[m]] not in (None, "")}
            if pairs and all(_is_time_part(tv, v) for tv, v in pairs):
                measures.remove(m)
                counts["medidas_componente_tiempo"] += 1

    key_cols = _key_columns(rows, idx, t, candidates + geo_cols, counts)
    companions = _companions(rows, idx, key_cols, candidates + geo_cols)
    seasonal = next(
        (
            c
            for c in key_cols
            if c in plan.var_labels
            and SEASONAL_LABEL_RE.search(plan.var_labels[c])
            and is_boolean({r[idx[c]] for r in rows})
        ),
        None,
    )
    units_col = next(
        (c for c in key_cols if c in plan.var_labels and UNITS_LABEL_RE.search(plan.var_labels[c])),
        None,
    )
    prefix = dataset_path(plan.unit_id)
    multi = bool(plan.measures)
    series: dict[str, SeriesData] = {}
    for r in rows:
        freq, fmt, d = parse_time(r[t])
        parts = [f"{c}={r[idx[c]]}" for c in key_cols]
        for m in measures:
            sid = ":".join([prefix, m, *parts])
            s = series.get(sid)
            if s is None:
                s = series[sid] = SeriesData(
                    title=_title(
                        plan, r, idx, key_cols, seasonal, label_cols, companions, m, multi
                    ),
                    units=_text(r, idx, units_col, label_cols, companions)
                    if units_col
                    else (plan.var_labels[m] if multi else None),
                    seasonal_adjustment=(
                        ("SA" if is_true(r[idx[seasonal]]) else "NSA") if seasonal else None
                    ),
                )
            s.formats.add(fmt)
            if d is None:
                counts["tiempo_no_reconocido"] += 1
                continue
            if d in s.obs:
                counts["observaciones_duplicadas"] += 1
                continue
            v = parse_number(r[idx[m]])
            if v is None:
                counts["valores_no_numericos"] += 1
            s.obs[d] = v
    counts["series"] += len(series)
    return ChunkResult(series, counts)


def _is_time_part(time_value: str, value: Any) -> bool:
    v = parse_number(value)
    if v is None:
        return False
    _, fmt, d = parse_time(time_value)
    return d is not None and (v == d.year or (fmt != "YYYY" and v == d.month))


def _key_columns(
    rows: list[list[Any]], idx: dict[str, int], t: int, cols: list[str], counts: Counter[str]
) -> list[str]:
    """Columnas que identifican una serie: sin constantes, sin componentes de tiempo y sin
    columnas determinadas por otra ya elegida (descripción de un código)."""
    if not rows:
        return []
    distinct = {c: {r[idx[c]] for r in rows} for c in cols}
    live = [c for c in cols if len(distinct[c]) > 1]
    counts["dimensiones_constantes"] += len(cols) - len(live)
    by_time = [(r[t], *(r[idx[c]] for c in live)) for r in rows]
    by_time_set = list(set(by_time))
    timeish = {c for i, c in enumerate(live, 1) if _determines(by_time_set, 0, i)}
    counts["dimensiones_de_tiempo"] += len(timeish)
    live = [c for c in live if c not in timeish]
    combos = list({tuple(r[idx[c]] for c in live) for r in rows})

    def avg_len(c: str) -> float:
        vals = distinct[c]
        return sum(len(str(v)) for v in vals) / len(vals)

    order = sorted(range(len(live)), key=lambda i: (avg_len(live[i]), live[i]))
    kept: list[int] = []
    for i in order:
        if any(_determines(combos, k, i) for k in kept):
            counts["dimensiones_redundantes"] += 1
            continue
        kept.append(i)
    return sorted(live[i] for i in kept)


def _companions(
    rows: list[list[Any]], idx: dict[str, int], key_cols: list[str], cols: list[str]
) -> dict[str, str]:
    """Para cada columna clave, la columna no clave más larga que le corresponde 1:1."""
    out: dict[str, str] = {}
    others = [c for c in cols if c not in key_cols]
    if not rows or not others:
        return out
    for k in key_cols:
        best: tuple[float, str] | None = None
        for o in others:
            pairs = list({(r[idx[k]], r[idx[o]]) for r in rows})
            if _determines(pairs, 0, 1) and _determines(pairs, 1, 0):
                length = sum(len(str(p[1])) for p in pairs) / len(pairs)
                if best is None or length > best[0]:
                    best = (length, o)
        if best is not None:
            out[k] = best[1]
    return out


def _text(
    r: list[Any],
    idx: dict[str, int],
    col: str,
    label_cols: dict[str, str],
    companions: dict[str, str],
) -> str:
    code = r[idx[col]]
    label_col = label_cols.get(col) or companions.get(col)
    label = r[idx[label_col]] if label_col else None
    return f"{label} ({code})" if label and label != code else str(code)


def _title(
    plan: Plan,
    r: list[Any],
    idx: dict[str, int],
    key_cols: list[str],
    seasonal: str | None,
    label_cols: dict[str, str],
    companions: dict[str, str],
    measure: str,
    multi: bool,
) -> str:
    parts = [plan.title]
    if multi:
        parts.append(plan.var_labels.get(measure, measure))
    for c in key_cols:
        if c == seasonal:  # SA/NSA es variante de la misma variable (regla de variantes)
            continue
        parts.append(f"{plan.var_labels.get(c, c)}: {_text(r, idx, c, label_cols, companions)}")
    return " | ".join(parts)


def parse_modified(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip()[:19]).replace(tzinfo=UTC)
    except ValueError:
        return None


def series_digest(series: dict[str, SeriesData]) -> str:
    h = hashlib.sha256()
    for sid in sorted(series):
        for d, v in sorted(series[sid].obs.items()):
            h.update(f"{sid}|{d.isoformat()}|{v!r}\n".encode())
    return h.hexdigest()[:16]


# --- adaptador ------------------------------------------------------------------------------


class CensusAdapter:
    """`SourceAdapter` de Census (ver docstring del módulo)."""

    source = SOURCE

    def __init__(
        self,
        client: ApiClient,
        probe_client: ApiClient,
        cfg: dict[str, Any],
        paths: SourcePaths,
        *,
        secret: str | None = None,
        today: Callable[[], date] = date.today,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.probe_client = probe_client
        self.scfg = {**DEFAULTS, **cfg["sources"][SOURCE]}
        self.paths = paths
        self._secret = secret
        self._today = today
        self.log = logger or logging.getLogger(__name__)
        self._datasets: list[dict[str, Any]] | None = None
        self._cache: OrderedDict[tuple[str, str | None], Loaded] = OrderedDict()
        self.stats: Counter[str] = Counter()

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any],
        paths: SourcePaths,
        api_key: str,
        logger: logging.Logger | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        today: Callable[[], date] = date.today,
        sleep: Callable[[float], None] = time.sleep,
    ) -> CensusAdapter:
        scfg = {**DEFAULTS, **cfg["sources"][SOURCE]}
        auth = {"key": api_key} if api_key else {}
        shared: dict[str, Any] = {
            "requests_per_minute": scfg["requests_per_minute"],
            "backoff_base_s": scfg["backoff_base_s"],
            "raw_dir": paths.raw / SOURCE,
            "transport": transport,
            "logger": logger,
            "sleep": sleep,
            "limiter": RateLimiter(scfg["requests_per_minute"], sleep=sleep),
        }
        client = ApiClient(
            BASE_URL, auth, max_retries=scfg["max_retries"], timeout_s=scfg["timeout_s"], **shared
        )
        probe = ApiClient(
            BASE_URL,
            auth,
            max_retries=scfg["probe_max_retries"],
            timeout_s=scfg["probe_timeout_s"],
            **shared,
        )
        return cls(client, probe, cfg, paths, secret=api_key, today=today, logger=logger)

    # --- contrato ----------------------------------------------------------------------------

    def list_units(self) -> list[Unit]:
        skipped = self._load_skipped()
        units = []
        for ds in self._catalog():
            url = (ds.get("distribution") or [{}])[0].get("accessURL") or ""
            endpoint = urlsplit(url).path
            unit_id = endpoint.removeprefix("/data/").strip("/")
            if not unit_id:
                continue
            modified = ds.get("modified")
            transient = skipped.get(unit_id, {}).get("transient", False)
            units.append(
                Unit(
                    unit_id,
                    None if transient else modified,
                    extra={
                        "endpoint": endpoint,
                        "title": str(ds.get("title") or unit_id),
                        "modified": modified,
                        "variables": urlsplit(ds.get("c_variablesLink") or "").path,
                        "geography": urlsplit(ds.get("c_geographyLink") or "").path,
                    },
                )
            )
        return sorted(units, key=lambda u: u.unit_id)

    def catalog_rows(self, unit: Unit) -> Iterator[dict[str, Any]]:
        loaded = self._load(unit)
        if loaded.skip_reason:
            return
        geo_level, geo_detail = loaded.geo_level, loaded.geo_detail
        last_updated = parse_modified(unit.extra.get("modified"))
        for sid in sorted(loaded.series):
            s = loaded.series[sid]
            freq, fmt = _series_frequency(s)
            dates = [d for d, v in s.obs.items() if v is not None]
            yield {
                "series_id": sid,
                "title": s.title,
                "frequency": freq,
                "frequency_raw": fmt,
                "units": s.units,
                "seasonal_adjustment": s.seasonal_adjustment,
                "observation_start": min(dates) if dates and freq else None,
                "observation_end": max(dates) if dates and freq else None,
                "last_updated": last_updated,
                "discontinued": False,
                "copyrighted": False,
                "geo_level": geo_level,
                "geo_detail": geo_detail,
                "category_path": unit.unit_id,
            }

    def values_version(self, unit: Unit) -> str | None:
        loaded = self._load(unit)
        if loaded.skip_reason or loaded.digest is None:
            return None
        return f"{unit.extra.get('modified') or ''}|{loaded.digest}"

    def fetch_values(self, unit: Unit, series_ids: set[str]) -> Iterator[dict[str, Any]]:
        loaded = self._load(unit)
        for sid in sorted(series_ids & loaded.series.keys()):
            s = loaded.series[sid]
            freq, _ = _series_frequency(s)
            if freq is None:
                continue
            for d, v in sorted(s.obs.items()):
                yield {
                    "series_id": sid,
                    "date": d,
                    "value": v,
                    "frequency": freq,
                    "units": s.units,
                    "seasonal_adjustment": s.seasonal_adjustment,
                }

    def close(self) -> None:
        self.client.close()
        self.probe_client.close()

    # --- internos ----------------------------------------------------------------------------

    def _catalog(self) -> list[dict[str, Any]]:
        if self._datasets is None:
            data = self.client.get_json(CATALOG_ENDPOINT, {})
            self._datasets = [d for d in data.get("dataset", []) if d.get("c_isTimeseries")]
        return self._datasets

    def _load(self, unit: Unit) -> Loaded:
        key = (unit.unit_id, unit.extra.get("modified"))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        loaded = self._load_uncached(unit)
        self._cache[key] = loaded
        while len(self._cache) > self.scfg["cache_units"]:
            self._cache.popitem(last=False)
        self._record_skip(unit, loaded)
        return loaded

    def _load_uncached(self, unit: Unit) -> Loaded:
        ex = unit.extra
        try:
            variables = self.client.get_json(ex["variables"], {})
            geography = self.client.get_json(ex["geography"], {})
        except ApiError as exc:
            return self._skip("metadatos_" + self._reason(exc), transient=True)
        plan = plan_dataset(
            unit.unit_id, ex["endpoint"], ex["title"], ex["modified"], variables, geography
        )
        counts: Counter[str] = Counter(
            medidas_excluidas=plan.measures_excluded,
            anotaciones_excluidas=plan.annotations_excluded,
            niveles_geo_omitidos=plan.geo_levels_omitted,
        )
        if plan.skip_reason:
            return self._skip(plan.skip_reason, counts=counts)
        chunks = get_chunks(plan, self.scfg["max_get_vars"])
        if not chunks:
            return self._skip("demasiadas_dimensiones", counts=counts)
        if len(chunks) > self.scfg["max_requests_per_unit"]:
            return self._skip(f"excede_solicitudes:{len(chunks)}", counts=counts)

        # Sondeo barato: periodo reciente del primer bloque.
        probe_from = f"from {self._today().year - 1}"
        try:
            header, rows = self._get_data(self.probe_client, plan, chunks[0], probe_from)
        except ApiError as exc:
            transient = exc.status is None or exc.status >= 500
            return self._skip("sondeo_" + self._reason(exc), transient=transient, counts=counts)
        if rows:
            probe = parse_chunk(plan, header, rows)
            n_probe = len(probe.series) * len(chunks)
            if n_probe > self.scfg["max_series_per_unit"]:
                return self._skip(f"excede_series:{n_probe}", counts=counts)

        series: dict[str, SeriesData] = {}
        n_rows = 0
        for chunk in chunks:
            try:
                header, rows = self._get_data(self.client, plan, chunk, START_PREDICATE)
            except ApiError as exc:
                transient = exc.status is None or exc.status >= 500
                return self._skip("datos_" + self._reason(exc), transient=transient, counts=counts)
            n_rows += len(rows)
            if n_rows > self.scfg["max_rows_per_unit"]:
                return self._skip(f"excede_filas:{n_rows}", counts=counts)
            res = parse_chunk(plan, header, rows)
            if rows and res.counts["sin_variable_de_valor"]:
                return self._skip("sin_variable_de_valor", counts=counts)
            counts += res.counts
            series.update(res.series)
            # El sondeo no ve datasets sin datos recientes: el tope se vuelve a mirar aquí.
            if len(series) > self.scfg["max_series_per_unit"]:
                return self._skip(f"excede_series:{len(series)}", counts=counts)
        counts["filas"] += n_rows
        counts["solicitudes"] += len(chunks) + 1
        self.stats += counts
        self.log.info(
            "dataset recorrido",
            extra={"fields": {"unit": unit.unit_id, "series": len(series), **counts}},
        )
        return Loaded(
            series,
            counts,
            series_digest(series),
            geo_level=plan.geo_level,
            geo_detail=plan.geo_detail,
        )

    def _get_data(
        self, client: ApiClient, plan: Plan, chunk: list[str], time_pred: str
    ) -> tuple[list[str], list[list[Any]]]:
        params: dict[str, Any] = {"get": ",".join(chunk), TIME_VAR: time_pred}
        if plan.geo_for:
            params["for"] = plan.geo_for
        try:
            data = client.get_json(plan.endpoint, params)
        except ApiError as exc:
            if exc.status == 204:  # sin datos en el rango pedido
                return [], []
            raise
        except ValueError as exc:  # cuerpo que no es JSON
            raise ApiError(f"{plan.endpoint}: respuesta no JSON", 200) from exc
        if not isinstance(data, list) or not data:
            return [], []
        return [str(h) for h in data[0]], data[1:]

    def _reason(self, exc: ApiError) -> str:
        msg = self._redact(str(exc))
        return f"http_{exc.status}:{msg[:200]}" if exc.status else f"red:{msg[:200]}"

    def _redact(self, text: str) -> str:
        return text.replace(self._secret, "***") if self._secret else text

    def _skip(
        self, reason: str, *, transient: bool = False, counts: Counter[str] | None = None
    ) -> Loaded:
        return Loaded({}, counts or Counter(), None, self._redact(reason), transient)

    # --- unidades no recorridas --------------------------------------------------------------

    @property
    def skipped_path(self) -> Path:
        return self.paths.processed / "ingest_state" / f"{SOURCE}_skipped.json"

    def _load_skipped(self) -> dict[str, dict[str, Any]]:
        p = self.skipped_path
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _record_skip(self, unit: Unit, loaded: Loaded) -> None:
        skipped = self._load_skipped()
        if loaded.skip_reason is None:
            if unit.unit_id not in skipped:
                return
            skipped.pop(unit.unit_id)
        else:
            self.stats["unidades_no_recorridas"] += 1
            self.log.warning(
                "unidad no recorrida",
                extra={"fields": {"unit": unit.unit_id, "motivo": loaded.skip_reason}},
            )
            skipped[unit.unit_id] = {
                "motivo": loaded.skip_reason,
                "transient": loaded.transient,
                "version": unit.extra.get("modified"),
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        p = self.skipped_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(skipped, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(p)


def _series_frequency(s: SeriesData) -> tuple[str | None, str]:
    if len(s.formats) != 1:
        return None, "+".join(sorted(s.formats))
    fmt = next(iter(s.formats))
    for _, freq, name in TIME_FORMATS:
        if name == fmt:
            return freq, fmt
    return None, fmt


# --- CLI ------------------------------------------------------------------------------------


def make_adapter(cfg: dict[str, Any], paths: SourcePaths, logger: logging.Logger) -> CensusAdapter:
    api_key = os.environ.get(KEY_ENV)
    if not api_key:
        print(f"{KEY_ENV} no está definida (ver .env.example)", file=sys.stderr)
        raise SystemExit(2)
    return CensusAdapter.from_config(cfg, paths, api_key, logger)


def main(argv: list[str] | None = None) -> int:
    """`uv run python -m src.ingest.census crawl|download|update|estimate`."""
    return common.main(argv, SOURCE, make_adapter)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
