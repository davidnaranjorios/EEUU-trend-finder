"""Adaptador BEA (Fase 4): recorrido genérico de la API de datos de BEA.

Uso: `uv run python -m src.ingest.bea crawl [--full] | download | update | estimate`.

Recorrido (sin listas de datasets ni de tablas, principio 1):

1. `GETDATASETLIST` da los datasets.
2. `GetParameterList` de cada dataset dice cómo pedirlo. Las reglas miran solo la estructura
   de la API (nombres y banderas de parámetros), no temas:
   - sin parámetro de año, el dataset no es de series de tiempo y se omite (con log);
   - el parámetro de unidad es el que identifica tablas (`Table*`, prefiriendo el obligatorio
     y el nombre sobre el id); si no hay, la primera dimensión que no es año, frecuencia ni
     geografía;
   - los demás parámetros con `AllValue` se piden con ese valor; los obligatorios sin
     `AllValue` se piden con `ALL` (si el dataset no lo acepta, BEA responde con un error que
     se registra por unidad y la unidad no produce series);
   - frecuencia: `AllValue` o la lista de valores de `GetParameterValues`.
3. `GetParameterValues` del parámetro de unidad da las tablas. Unidad = `<dataset>:<tabla>`.

Datasets con parámetro geográfico de códigos FIPS (hoy Regional): el catálogo pide el nivel
nacional (`GeoFips=00000`, todas las líneas en una solicitud) y el estatal (`GeoFips=STATE`,
una solicitud por línea, porque la API no acepta varias geografías con todas las líneas). No se
piden condados, áreas metropolitanas ni otros niveles: se cuentan (`geos_omitidas`) con una
solicitud `GetParameterValuesFiltered` por tabla. La descarga del grupo nacional solo necesita
la solicitud nacional.

BEA no da ETag:
- `catalog_version` es un hash de la descripción de la tabla y de los parámetros con que se
  pide: un `crawl` incremental solo reprocesa tablas nuevas o cambiadas, y el refresco completo
  (`catalog_refresh_days`) actualiza fechas y series nuevas de las demás.
- `values_version` es un hash de los datos devueltos (serie, fecha, valor): cuesta la misma
  solicitud que la descarga, pero evita reescribir datos sin cambios. Dentro de una corrida, la
  respuesta se cachea en memoria (LRU acotado por observaciones), así que `catalog_rows`,
  `values_version` y `fetch_values` de una misma unidad cuestan una sola solicitud.

Límites oficiales (BEA API User Guide): 100 solicitudes/min, 100 MB/min y 30 errores/min; al
excederlos, HTTP 429 con Retry-After. El ritmo de solicitudes lo da `ApiClient`; aquí se lleva
la cuenta de bytes y de errores (incluidos los errores que BEA devuelve dentro de un 200) en una
ventana deslizante de 60 s y se pausa antes de llegar al límite.

La API key (UserID) va solo en los parámetros de autenticación de `ApiClient`. BEA la devuelve
en `BEAAPI.Request`: ese bloque se elimina de toda respuesta antes de guardarla en raw, de
registrarla o de usarla en un mensaje de error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from src.ingest import common
from src.ingest.common import SourcePaths, Unit
from src.ingest.http import ApiClient, ApiError

SOURCE = "bea"
# Agencia que publica los datos, con el nombre que usa FRED en `release/sources`: el mismo dato
# bajado de FRED y de aquí cuenta como una sola fuente en la triangulación (ADR 0009).
AGENCY = "U.S. Bureau of Economic Analysis"
BASE_URL = "https://apps.bea.gov/api"
ENDPOINT = "data"
KEY_ENV = "BEA_API_KEY"

# Límites oficiales por minuto, con margen: se pausa antes de alcanzarlos.
BYTES_PER_MIN = 90_000_000  # oficial: 100 MB/min
ERRORS_PER_MIN = 25  # oficial: 30 errores/min
WINDOW_S = 60.0
# Código de error de BEA por exceso de límite (también llega dentro de un 200).
RATE_LIMIT_ERROR_CODE = "7"
# Observaciones que se mantienen en la caché en memoria de respuestas por unidad.
CACHE_MAX_OBS = 2_000_000

# Convenciones de la API (no temas): todas las líneas, código nacional y nivel estatal.
ALL = "ALL"
NATIONAL_FIPS = "00000"
STATE_LEVEL = "STATE"

# Reglas sobre nombres de parámetros y campos de la API.
YEAR_PARAM_RE = re.compile(r"(?i)^year$")
FREQ_PARAM_RE = re.compile(r"(?i)^frequency$")
TABLE_PARAM_RE = re.compile(r"(?i)^table")
GEO_PARAM_RE = re.compile(r"(?i)fips")
DESC_FIELD_RE = re.compile(r"(?i)desc")
# Campos de una fila de datos que no identifican la serie.
NON_KEY_FIELD_RE = re.compile(
    r"(?i)^(timeperiod|year|quarter|month|datavalue|noteref|cl_unit|unit_mult|frequency)$"
    r"|desc|name$"
)
SERIES_ID_FIELD_RE = re.compile(r"(?i)seriesid$")
GEO_FIELD_RE = re.compile(r"(?i)fips|area|country")
TOTAL_GEO_RE = re.compile(r"(?i)^(all|total)|^0+$")
UNIT_FIELD_RE = re.compile(r"(?i)^cl_unit$")
UNIT_MULT_FIELD_RE = re.compile(r"(?i)^unit_mult$")
NSA_RE = re.compile(r"(?i)not seasonally adjusted|^[A-Z]?NSA$|\bNSA\b")
SA_RE = re.compile(r"(?i)seasonally adjusted|^[A-Z]?SA$|\bSA\b")
LINE_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
LAST_REVISED_RE = re.compile(r"LastRevised:\s*([A-Za-z]+ \d{1,2}, \d{4})")

# Formatos de periodo de BEA: 2024, 2024Q3, 2024M07.
PERIOD_RE = re.compile(r"^(\d{4})(?:([QM])(\d{1,2}))?$")
ROMAN_QUARTERS = {"I": 1, "II": 2, "III": 3, "IV": 4}
# Valores no numéricos de BEA: (NA), (D), (L), (NM), "...", etc.
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")

log = logging.getLogger(__name__)


# --- funciones puras -------------------------------------------------------------------------


def scrub(data: Any) -> Any:
    """Quita `BEAAPI.Request` (BEA devuelve ahí el UserID) de una respuesta."""
    if isinstance(data, dict) and isinstance(data.get("BEAAPI"), dict):
        api = dict(data["BEAAPI"])
        api.pop("Request", None)
        return {**data, "BEAAPI": api}
    return data


def api_error(data: dict[str, Any]) -> tuple[str, str] | None:
    """(código, descripción) del error que BEA devuelve dentro de un 200, o None."""
    api = data.get("BEAAPI", {}) if isinstance(data, dict) else {}
    candidates = [api.get("Error")]
    for res in _as_list(api.get("Results")):
        if isinstance(res, dict):
            candidates.append(res.get("Error"))
    for err in candidates:
        for e in _as_list(err):
            if isinstance(e, dict):
                # Formato general (APIErrorCode/APIErrorDescription) o el de MNE (number/error).
                code = str(e.get("APIErrorCode", e.get("number", ""))).strip()
                desc = str(e.get("APIErrorDescription", e.get("error", ""))).strip()
                detail = e.get("ErrorDetail")
                if isinstance(detail, dict) and detail.get("Description"):
                    desc = f"{desc} {detail['Description']}".strip()
                return code, desc[:300]
    return None


def results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """`BEAAPI.Results` como lista de dicts (BEA devuelve dict o lista según el dataset)."""
    return [r for r in _as_list(data.get("BEAAPI", {}).get("Results")) if isinstance(r, dict)]


def param_values(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(clave, descripción) de una respuesta de GetParameterValues[Filtered].

    El nombre del campo clave cambia por dataset (Key, TableName, FrequencyID, key...): se
    toma el primer campo que no es descripción, y la descripción del primero que sí lo es.
    """
    out = []
    for res in results(data):
        for v in _as_list(res.get("ParamValue")):
            if not isinstance(v, dict) or not v:
                continue
            key = next((str(x) for k, x in v.items() if not DESC_FIELD_RE.search(k)), None)
            desc = next((str(x) for k, x in v.items() if DESC_FIELD_RE.search(k)), "")
            if key is not None and key.strip():
                out.append((key.strip(), " ".join(desc.split())))
    return out


def parse_value(raw: Any) -> float | None:
    """DataValue de BEA → float; "(NA)", "(D)" y similares → None."""
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    return float(s) if NUMBER_RE.match(s) else None


def parse_period(row: dict[str, Any]) -> tuple[str, date] | None:
    """(frecuencia, fecha de inicio del periodo) de una fila, o None si no hay periodo."""
    tp = row.get("TimePeriod")
    if tp is not None:
        m = PERIOD_RE.match(str(tp).strip())
        if not m:
            return None
        year, kind, num = int(m.group(1)), m.group(2), m.group(3)
        if kind is None:
            return "A", date(year, 1, 1)
        n = int(num)
        if kind == "Q" and 1 <= n <= 4:
            return "Q", date(year, 3 * n - 2, 1)
        if kind == "M" and 1 <= n <= 12:
            return "M", date(year, n, 1)
        return None
    year = str(row.get("Year", "")).strip()
    if not year.isdigit():
        return None
    quarter = str(row.get("Quarter", "")).strip().upper()
    month = str(row.get("Month", "")).strip()
    if quarter in ROMAN_QUARTERS:
        return "Q", date(int(year), 3 * ROMAN_QUARTERS[quarter] - 2, 1)
    if quarter.isdigit() and 1 <= int(quarter) <= 4 and quarter != year:
        return "Q", date(int(year), 3 * int(quarter) - 2, 1)
    if month.isdigit() and 1 <= int(month) <= 12:
        return "M", date(int(year), int(month), 1)
    return "A", date(int(year), 1, 1)


def seasonal_adjustment(*texts: str | None) -> str | None:
    """SA / NSA si algún texto (código de frecuencia, tabla, unidades) lo dice; si no, None."""
    joined = [t for t in texts if t]
    if any(NSA_RE.search(t) for t in joined):
        return "NSA"
    if any(SA_RE.search(t) for t in joined):
        return "SA"
    return None


def units_of(row: dict[str, Any], fallback: str | None) -> str | None:
    unit = next((str(v).strip() for k, v in row.items() if UNIT_FIELD_RE.match(k)), None)
    mult = next((str(v).strip() for k, v in row.items() if UNIT_MULT_FIELD_RE.match(k)), None)
    if not unit:
        return fallback
    return f"{unit} (x10^{mult})" if mult and mult != "0" else unit


def geo_of(row: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """(geo_level, código geográfico o None si es el total, nombre legible)."""
    for k, v in row.items():
        if GEO_FIELD_RE.search(k) and not DESC_FIELD_RE.search(k) and not k.endswith("Name"):
            code = str(v).strip()
            if not code or TOTAL_GEO_RE.search(code):
                return "nacional", None, None
            # Nombre legible solo si la fila trae un campo *Name aparte (si no, la descripción
            # de la serie ya nombra el área).
            name = next(
                (str(x).strip() for kk, x in row.items() if kk.endswith("Name") and x), None
            )
            return ("regional" if code.isdigit() else "internacional"), code, name
    return "nacional", None, None


def last_updated(res: dict[str, Any]) -> datetime | None:
    """Fecha de publicación si la respuesta la trae (TsLastUpdated o LastRevised en notas)."""
    ts = res.get("TsLastUpdated")
    if ts:
        try:
            return datetime.fromisoformat(str(ts).strip()).replace(microsecond=0, tzinfo=UTC)
        except ValueError:
            pass
    for note in _as_list(res.get("Notes")):
        m = LAST_REVISED_RE.search(str(note.get("NoteText", "")) if isinstance(note, dict) else "")
        if m:
            try:
                return datetime.strptime(m.group(1), "%B %d, %Y").replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def _as_list(x: Any) -> list[Any]:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _clean(s: str) -> str:
    return re.sub(r"[:\s]+", "_", s.strip())


@dataclass
class SeriesAcc:
    title: str
    frequency: str
    frequency_raw: str
    units: str | None
    seasonal_adjustment: str | None
    geo_level: str
    geo_detail: str | None
    obs: dict[date, float | None] = field(default_factory=dict)

    @property
    def n_obs(self) -> int:
        return len(self.obs)


@dataclass
class Parsed:
    """Series de una respuesta de GetData, agrupadas."""

    series: dict[str, SeriesAcc]
    last_updated: datetime | None
    stats: Counter[str]

    @property
    def n_obs(self) -> int:
        return sum(s.n_obs for s in self.series.values())


def parse_data(
    data: dict[str, Any],
    *,
    dataset: str,
    table: str,
    table_desc: str,
    unit_param: str,
    skip_national: bool = False,
    key_titles: dict[str, str] | None = None,
) -> Parsed:
    """Agrupa las filas de GetData por serie.

    `series_id = <dataset>:<tabla>:<clave>[:<geo>]:<frecuencia>`. La clave es el id de serie de
    BEA si la fila lo trae (`*SeriesId`); si no, los campos que identifican la fila (código de
    serie, línea, industria...), sin el prefijo de la tabla. El código geográfico se añade solo
    si no es el total nacional.
    """
    stats: Counter[str] = Counter()
    series: dict[str, SeriesAcc] = {}
    base_titles: dict[str, set[str]] = {}
    updated = None
    for res in results(data):
        updated = updated or last_updated(res)
        fallback_units = res.get("UnitOfMeasure")
        if fallback_units and str(fallback_units).lower().startswith("see"):
            fallback_units = None
        for row in _as_list(res.get("Data")):
            if not isinstance(row, dict):
                continue
            stats["filas"] += 1
            period = parse_period(row)
            if period is None:
                stats["filas_sin_periodo"] += 1
                continue
            freq, d = period
            freq_raw = str(row.get("Frequency") or freq).strip()
            geo_level, geo_code, geo_name = geo_of(row)
            if skip_national and geo_level == "nacional":
                continue
            sid_field = next((k for k in row if SERIES_ID_FIELD_RE.search(k)), None)
            if sid_field is not None:
                key = _clean(str(row[sid_field]))
            else:
                parts = [
                    str(v).strip()
                    for k, v in row.items()
                    if k.lower() != unit_param.lower()
                    and not NON_KEY_FIELD_RE.search(k)
                    and not GEO_FIELD_RE.search(k)
                ]
                parts = [p.removeprefix(f"{table}-") for p in parts if p]
                key = ".".join(_clean(p) for p in parts) or "0"
            geo_part = f":{_clean(geo_code)}" if geo_code and sid_field is None else ""
            sid = f"{dataset}:{table}:{key}{geo_part}:{_clean(freq_raw)}"
            value = parse_value(row.get("DataValue"))
            if value is None:
                stats["valores_no_numericos"] += 1
            acc = series.get(sid)
            if acc is None:
                desc = next(
                    (" ".join(str(v).split()) for k, v in row.items() if DESC_FIELD_RE.search(k)),
                    "",
                )
                title = desc or (key_titles or {}).get(key) or key
                if geo_name:
                    title = f"{title}, {geo_name}"
                title = f"{title} — {table_desc}" if table_desc else title
                units = units_of(row, fallback_units)
                acc = SeriesAcc(
                    title=title,
                    frequency=freq,
                    frequency_raw=freq_raw,
                    units=units,
                    seasonal_adjustment=seasonal_adjustment(freq_raw, table_desc, units),
                    geo_level=geo_level,
                    geo_detail=geo_code,
                )
                series[sid] = acc
                base_titles.setdefault(title, set()).add(f"{key}{geo_part}")
            if d in acc.obs:
                stats["observaciones_duplicadas"] += 1
            acc.obs[d] = value
    # Líneas distintas con la misma descripción (p. ej. un subtotal repetido en dos bloques de
    # la tabla) no son variantes de una misma variable: se desambigua el título con la clave.
    for sid, acc in series.items():
        keys = base_titles.get(acc.title, set())
        if len(keys) > 1:
            acc.title = f"{acc.title} [{sid.split(':')[2]}]"
    stats["series"] = len(series)
    return Parsed(series, updated, stats)


# --- cliente ---------------------------------------------------------------------------------


class BeaApiClient(ApiClient):
    """`ApiClient` que nunca guarda en raw el bloque `Request` (lleva el UserID)."""

    def _save_raw(
        self, path: Path | None, endpoint: str, params: dict[str, Any], data: dict[str, Any]
    ) -> None:
        super()._save_raw(path, endpoint, params, scrub(data))


@dataclass
class DatasetPlan:
    """Cómo pedir un dataset, deducido de su GetParameterList."""

    name: str
    desc: str
    unit_param: str
    params: dict[str, str]
    geo_param: str | None = None
    line_param: str | None = None


class BeaAdapter:
    source = SOURCE

    def __init__(
        self,
        client: ApiClient,
        *,
        api_key: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
        max_retries: int = 6,
        cache_max_obs: int = CACHE_MAX_OBS,
    ) -> None:
        self.client = client
        self._key = api_key
        self._clock = clock
        self._sleep = sleep
        self.log = logger or log
        self.max_retries = max_retries
        self.cache_max_obs = cache_max_obs
        self._bytes: deque[tuple[float, int]] = deque()
        self._errors: deque[float] = deque()
        self._cache: OrderedDict[tuple[str, str], Parsed] = OrderedDict()
        self._units: list[Unit] | None = None
        self.stats: Counter[str] = Counter()
        self.skipped: dict[str, str] = {}

    # --- solicitudes ---------------------------------------------------------------------

    def _request(self, method: str, **params: str) -> dict[str, Any] | None:
        """JSON de un método de la API; None (y log) si BEA devuelve un error en el 200."""
        query = {"method": method, "ResultFormat": "JSON", **params}
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                data = scrub(self.client.get_json(ENDPOINT, query))
            except ApiError as exc:
                self._note_error()
                raise ApiError(self._redact(str(exc)), exc.status) from None
            self.stats["solicitudes"] += 1
            size = len(json.dumps(data, separators=(",", ":")))
            self._bytes.append((self._clock(), size))
            self.stats["bytes"] += size
            err = api_error(data)
            if err is None:
                return data
            self._note_error()
            code, desc = err
            if code == RATE_LIMIT_ERROR_CODE and attempt < self.max_retries:
                self.stats["limite_en_200"] += 1
                self.log.warning(
                    "límite de BEA en respuesta 200; pausa",
                    extra={"fields": {"method": method, "code": code, "delay_s": WINDOW_S}},
                )
                self._sleep(WINDOW_S)
                continue
            self.stats[f"error_api_{code or 'sin_codigo'}"] += 1
            self.log.warning(
                "error de BEA en respuesta 200",
                extra={"fields": {"method": method, "params": params, "code": code, "error": desc}},
            )
            return None
        return None

    def _note_error(self) -> None:
        self._errors.append(self._clock())

    def _throttle(self) -> None:
        """Pausa si en los últimos 60 s se acerca a 100 MB o a 30 errores."""
        while True:
            now = self._clock()
            while self._bytes and now - self._bytes[0][0] >= WINDOW_S:
                self._bytes.popleft()
            while self._errors and now - self._errors[0] >= WINDOW_S:
                self._errors.popleft()
            waits = []
            if self._bytes and sum(b for _, b in self._bytes) >= BYTES_PER_MIN:
                waits.append(WINDOW_S - (now - self._bytes[0][0]))
            if len(self._errors) >= ERRORS_PER_MIN:
                waits.append(WINDOW_S - (now - self._errors[0]))
            if not waits:
                return
            delay = max(0.1, max(waits))
            self.stats["pausas_por_limite"] += 1
            self.log.info("pausa por límite de BEA", extra={"fields": {"delay_s": delay}})
            self._sleep(delay)

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    # --- recorrido ---------------------------------------------------------------------------

    def list_units(self) -> list[Unit]:
        if self._units is not None:
            return self._units
        units: list[Unit] = []
        data = self._request("GETDATASETLIST")
        datasets = []
        for res in results(data or {}):
            for d in _as_list(res.get("Dataset")):
                if isinstance(d, dict) and d.get("DatasetName"):
                    datasets.append((str(d["DatasetName"]), str(d.get("DatasetDescription", ""))))
        for name, desc in datasets:
            plan = self._plan(name, desc)
            if plan is None:
                continue
            values = param_values(
                self._request("GetParameterValues", DatasetName=name, ParameterName=plan.unit_param)
                or {}
            )
            if not values:
                self._skip(name, f"sin valores para el parámetro de unidad {plan.unit_param}")
                continue
            signature = json.dumps(
                [plan.unit_param, plan.params, plan.geo_param, plan.line_param], sort_keys=True
            )
            for key, tdesc in values:
                version = hashlib.sha1(f"{signature}|{key}|{tdesc}".encode()).hexdigest()[:16]
                units.append(
                    Unit(
                        f"{name}:{key}",
                        version,
                        extra={"plan": plan, "table": key, "table_desc": tdesc},
                    )
                )
            self.stats[f"unidades_{name}"] = len(values)
        self.log.info(
            "unidades de BEA",
            extra={
                "fields": {
                    "datasets": len(datasets),
                    "unidades": len(units),
                    "omitidos": self.skipped,
                }
            },
        )
        self._units = units
        return units

    def _skip(self, dataset: str, reason: str) -> None:
        self.skipped[dataset] = reason
        self.log.info("dataset omitido", extra={"fields": {"dataset": dataset, "motivo": reason}})

    def _plan(self, name: str, desc: str) -> DatasetPlan | None:
        data = self._request("GetParameterList", DatasetName=name)
        params = [p for res in results(data or {}) for p in _as_list(res.get("Parameter")) if p]
        params = [p for p in params if isinstance(p, dict) and p.get("ParameterName")]
        if not params:
            self._skip(name, "GetParameterList sin parámetros")
            return None

        def pname(p: dict[str, Any]) -> str:
            return str(p["ParameterName"]).strip()

        def required(p: dict[str, Any]) -> bool:
            return str(p.get("ParameterIsRequiredFlag", "0")).strip() == "1"

        def all_value(p: dict[str, Any]) -> str:
            return str(p.get("AllValue") or "").strip()

        year = next((p for p in params if YEAR_PARAM_RE.match(pname(p))), None)
        if year is None:
            self._skip(name, "sin parámetro de año: no es un dataset de series de tiempo")
            return None
        freq = next((p for p in params if FREQ_PARAM_RE.match(pname(p))), None)
        geo = next((p for p in params if GEO_PARAM_RE.search(pname(p))), None)
        tables = sorted(
            (p for p in params if TABLE_PARAM_RE.match(pname(p))),
            key=lambda p: (not required(p), "id" in pname(p).lower()),
        )
        special = {id(year), id(freq), id(geo)}
        dims = [p for p in params if id(p) not in special]
        unit_p = tables[0] if tables else (dims[0] if dims else None)
        if unit_p is None:
            self._skip(name, "sin parámetro que identifique tablas o dimensiones")
            return None
        query: dict[str, str] = {pname(year): all_value(year) or ALL}
        if freq is not None:
            # La lista explícita de frecuencias: el AllValue anunciado no siempre se acepta
            # en GetData (p. ej. "Invalid frequency: ALL"). Si no hay lista, el AllValue.
            values = param_values(
                self._request("GetParameterValues", DatasetName=name, ParameterName=pname(freq))
                or {}
            )
            fv = ",".join(k for k, _ in values) or all_value(freq)
            if fv:
                query[pname(freq)] = fv
        line_param = None
        for p in dims:
            if p is unit_p or pname(p) in {pname(t) for t in tables}:
                continue
            if all_value(p):
                query[pname(p)] = all_value(p)
            elif required(p):
                query[pname(p)] = ALL
                if geo is not None and line_param is None:
                    line_param = pname(p)
        return DatasetPlan(
            name=name,
            desc=desc,
            unit_param=pname(unit_p),
            params=query,
            geo_param=pname(geo) if geo is not None else None,
            line_param=line_param,
        )

    # --- datos por unidad --------------------------------------------------------------------

    def _get(self, unit: Unit, level: str) -> Parsed:
        """Series de la unidad en un nivel ("nacional" o "estatal"), con caché LRU."""
        ck = (unit.unit_id, level)
        if ck in self._cache:
            self._cache.move_to_end(ck)
            return self._cache[ck]
        plan: DatasetPlan = unit.extra["plan"]
        table, tdesc = unit.extra["table"], unit.extra["table_desc"]
        base = {
            "DatasetName": plan.name,
            plan.unit_param: table,
            **plan.params,
        }
        kw = {
            "dataset": plan.name,
            "table": table,
            "table_desc": tdesc,
            "unit_param": plan.unit_param,
        }
        if level == "nacional":
            if plan.geo_param:
                base[plan.geo_param] = NATIONAL_FIPS
            data = self._request("GetData", **base)
            parsed = parse_data(data or {}, **kw)
            if data is None:
                parsed.stats["error"] += 1
        else:
            parsed = self._get_state_level(unit, plan, base, kw)
        self._cache[ck] = parsed
        while (
            sum(p.n_obs for p in self._cache.values()) > self.cache_max_obs and len(self._cache) > 1
        ):
            self._cache.popitem(last=False)
        return parsed

    def _get_state_level(
        self, unit: Unit, plan: DatasetPlan, base: dict[str, str], kw: dict[str, Any]
    ) -> Parsed:
        out = Parsed({}, None, Counter())
        if not plan.geo_param or not plan.line_param:
            return out
        table = unit.extra["table"]
        filt = {"DatasetName": plan.name, plan.unit_param: table}
        lines = param_values(
            self._request("GetParameterValuesFiltered", TargetParameter=plan.line_param, **filt)
            or {}
        )
        geos = param_values(
            self._request("GetParameterValuesFiltered", TargetParameter=plan.geo_param, **filt)
            or {}
        )
        # Las filas por línea no traen descripción: se toma la de la lista de líneas.
        titles = {k: LINE_PREFIX_RE.sub("", d) for k, d in lines}
        # Niveles no pedidos (condados, áreas metropolitanas...): se cuentan, no se piden.
        out.stats["geos_disponibles"] = len(geos)
        n_states = 0
        for line, _ in lines:
            q = {**base, plan.geo_param: STATE_LEVEL, plan.line_param: line}
            data = self._request("GetData", **q)
            if data is None:
                out.stats["lineas_con_error"] += 1
                continue
            p = parse_data(data, skip_national=True, key_titles=titles, **kw)
            out.series.update(p.series)
            out.stats += p.stats
            out.last_updated = out.last_updated or p.last_updated
            n_states = max(n_states, len({s.geo_detail for s in p.series.values()}))
        out.stats["lineas"] = len(lines)
        out.stats["geos_omitidas"] = max(0, len(geos) - n_states - 1) if geos else 0
        return out

    def catalog_rows(self, unit: Unit) -> Iterator[dict[str, Any]]:
        plan: DatasetPlan = unit.extra["plan"]
        levels = ["nacional"] + (["estatal"] if plan.geo_param and plan.line_param else [])
        n = 0
        for level in levels:
            parsed = self._get(unit, level)
            if level == "nacional" and not parsed.series:
                reason = "error de la API" if parsed.stats["error"] else "respuesta sin periodos"
                self.log.info(
                    "unidad sin series de tiempo",
                    extra={
                        "fields": {
                            "unit": unit.unit_id,
                            "motivo": reason,
                            "filas": parsed.stats["filas"],
                        }
                    },
                )
                self.stats["unidades_sin_series"] += 1
                return
            if level == "estatal":
                self.stats["geos_omitidas"] += parsed.stats["geos_omitidas"]
                self.log.info(
                    "nivel estatal",
                    extra={"fields": {"unit": unit.unit_id, **dict(parsed.stats)}},
                )
            for sid, acc in parsed.series.items():
                dated = [d for d, v in acc.obs.items() if v is not None]
                n += 1
                yield {
                    "series_id": sid,
                    "title": acc.title,
                    "frequency": acc.frequency,
                    "frequency_raw": acc.frequency_raw,
                    "units": acc.units,
                    "seasonal_adjustment": acc.seasonal_adjustment,
                    "observation_start": min(dated) if dated else None,
                    "observation_end": max(dated) if dated else None,
                    "last_updated": parsed.last_updated,
                    "discontinued": False,
                    "copyrighted": False,
                    "geo_level": acc.geo_level,
                    "geo_detail": acc.geo_detail,
                    "category_path": f"{plan.desc} > {unit.extra['table_desc']}",
                }

    def values_version(self, unit: Unit) -> str | None:
        """Hash de los datos nacionales de la unidad (BEA no da ETag)."""
        parsed = self._get(unit, "nacional")
        if not parsed.series:
            return None
        h = hashlib.sha1()
        for sid in sorted(parsed.series):
            for d, v in sorted(parsed.series[sid].obs.items()):
                h.update(f"{sid}|{d.isoformat()}|{v!r}\n".encode())
        return h.hexdigest()[:20]

    def fetch_values(self, unit: Unit, series_ids: set[str]) -> Iterator[dict[str, Any]]:
        parsed = self._get(unit, "nacional")
        pending = set(series_ids) - set(parsed.series)
        sources = [parsed]
        if pending and unit.extra["plan"].geo_param:
            sources.append(self._get(unit, "estatal"))
        for p in sources:
            for sid in sorted(series_ids & set(p.series)):
                acc = p.series[sid]
                for d, v in sorted(acc.obs.items()):
                    if v is None:
                        continue
                    yield {
                        "series_id": sid,
                        "date": d,
                        "value": v,
                        "frequency": acc.frequency,
                        "units": acc.units,
                        "seasonal_adjustment": acc.seasonal_adjustment,
                    }

    def close(self) -> None:
        self.log.info(
            "cliente BEA cerrado",
            extra={
                "fields": {
                    **dict(self.stats),
                    "solicitudes_http": self.client.n_requests,
                    "reintentos_http": self.client.n_retries,
                }
            },
        )
        self.client.close()


# --- construcción y CLI -----------------------------------------------------------------------


def build_adapter(
    scfg: dict[str, Any],
    raw_dir: Path | None,
    api_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> BeaAdapter:
    client = BeaApiClient(
        BASE_URL,
        {"UserID": api_key},
        requests_per_minute=scfg["requests_per_minute"],
        max_retries=scfg["max_retries"],
        timeout_s=scfg["timeout_s"],
        backoff_base_s=scfg["backoff_base_s"],
        raw_dir=raw_dir,
        transport=transport,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    return BeaAdapter(
        client,
        api_key=api_key,
        clock=clock,
        sleep=sleep,
        logger=logger,
        max_retries=scfg["max_retries"],
    )


def make_adapter(cfg: dict[str, Any], paths: SourcePaths, logger: logging.Logger) -> BeaAdapter:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise SystemExit(f"Falta {KEY_ENV} en el entorno (.env)")
    return build_adapter(cfg["sources"][SOURCE], paths.raw / SOURCE, key, logger=logger)


def main(argv: list[str] | None = None) -> int:
    return common.main(argv, SOURCE, make_adapter)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
