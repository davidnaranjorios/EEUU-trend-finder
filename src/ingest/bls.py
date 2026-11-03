"""Adaptador BLS (Fase 4) sobre los archivos masivos de https://download.bls.gov/pub/time.series/.

Decisión aprobada: archivos planos (LABSTAT) en vez de la API v2, que limita series por consulta
y consultas diarias. Cada encuesta es un directorio de 2–4 letras con:

- `<s>.series`: TSV con cabecera (columnas con espacios a recortar). Siempre trae `series_id`,
  `begin_year/begin_period/end_year/end_period`; casi siempre `seasonal`; muchas traen
  `series_title` y columnas `*_code` con su tabla de códigos `<s>.<dimensión>`.
- `<s>.data.*`: uno o varios TSV `series_id, year, period, value, footnote_codes`.

Unidad = encuesta (un directorio con `<s>.series`; `compressed/`, `sdmx/` y similares no lo
tienen y no son unidades). Nada aquí nombra encuestas, series ni dimensiones concretas: las
columnas se descubren en la cabecera y las tablas de códigos en el listado del directorio.

Uso: `uv run python -m src.ingest.bls crawl [--full] | download | update | estimate`.
El User-Agent (con contacto; download.bls.gov responde 403 sin él) se lee de la variable de
entorno nombrada en `sources.user_agent_env`. Nunca se registra su valor.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from src.ingest import common
from src.ingest.common import SOURCE_CATALOG, SourcePaths, Unit
from src.ingest.http import ApiClient
from src.ingest.store import read_table

SOURCE = "bls"
# Agencia que publica los datos, con el nombre que usa FRED en `release/sources`: el mismo dato
# bajado de FRED y de aquí cuenta como una sola fuente en la triangulación (ADR 0009).
AGENCY = "U.S. Bureau of Labor Statistics"
DEFAULT_BASE_URL = "https://download.bls.gov/pub/time.series/"
# Tablas de códigos más grandes que esto no se cargan en memoria (se registra en el log).
MAX_MAP_BYTES = 20_000_000

# Entrada de un listado de directorio IIS: fecha, hora, tamaño o <dir>, enlace.
_LISTING_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)\s+(\d+|&lt;dir&gt;)\s+"
    r'<A HREF="([^"]+)">([^<]+)</A>',
    re.IGNORECASE,
)
# Línea "CÓDIGO  Nombre" de overview.txt (solo se aceptan códigos que son encuestas).
_OVERVIEW_RE = re.compile(r"^\s*([A-Za-z]{2,4})\s+(\S.*?)\s*$")
_PERIOD_RE = re.compile(r"^([A-Z])(\d{2})$")
# Nombres estructurales de dimensión geográfica (no temas).
_GEO_COL_RE = re.compile(r"area|state|region|county|msa|fips|country")
_COUNTRY_COL_RE = re.compile(r"country")
# Texto de la fila de nivel país en una tabla de códigos geográfica.
_NATIONAL_RE = re.compile(
    r"\b(?!u\.?\s?s\.?\s*virgin)"
    r"(?:u\.\s?s\b|us\b|united states|national(?!\s+city)|nationwide|all states)",
    re.IGNORECASE,
)
# Texto neutro: "todo el estado" no resta nivel nacional si otra dimensión es nacional.
_NEUTRAL_GEO_RE = re.compile(r"^\s*statewide\s*$", re.IGNORECASE)
# Columna cuya tabla describe el tipo de dato (se usa como `units`).
_UNITS_COL_RE = re.compile(r"^(?:data_?type|measure)")
_TEXT_COL_RE = re.compile(r"_(?:text|name|title)$")
_CODE_SUFFIX = "_code"
_FOOTNOTE_PREFIX = "footnote"
_TITLE_COL = "series_title"
_END_PERIOD_COL = "end_period"
_SEASONAL_COL = "seasonal"
_ANNUAL = "annual_avg"  # M13, Q05, S03: promedio anual publicado junto a la serie
_SEASONAL_CODES = {"S": "SA", "U": "NSA"}


# --- periodos ---------------------------------------------------------------------------------


def period_info(period: str) -> tuple[str, int] | None:
    """(tipo, mes inicial) de un periodo BLS, o None si no se reconoce.

    M01–M12 → ("M", mes); Q01–Q04 → ("Q", primer mes del trimestre); A01 → ("A", 1);
    S01–S02 → ("S", 1|7) (semestral); M13, Q05 y S03 son promedios anuales (ver `<s>.period`
    de cada encuesta: "Annual Average") → (_ANNUAL, 1).
    """
    m = _PERIOD_RE.match(period.strip())
    if not m:
        return None
    letter, n = m.group(1), int(m.group(2))
    if letter == "M" and 1 <= n <= 12:
        return "M", n
    if letter == "Q" and 1 <= n <= 4:
        return "Q", 3 * n - 2
    if letter == "A" and n == 1:
        return "A", 1
    if letter == "S" and n in (1, 2):
        return "S", 6 * n - 5
    if (letter, n) in (("M", 13), ("Q", 5), ("S", 3)):
        return _ANNUAL, 1
    return None


def series_frequency(begin_period: str, end_period: str) -> str | None:
    """Frecuencia (código del esquema o "S" semestral) a partir de los periodos extremos.

    El periodo final manda; si es un promedio anual (M13...), decide el inicial; si ambos son
    promedios anuales, la serie es anual. None si no se reconoce ninguno.
    """
    kinds = [period_info(p) for p in (end_period, begin_period)]
    for k in kinds:
        if k is not None and k[0] != _ANNUAL:
            return k[0]
    if any(k is not None for k in kinds):
        return "A"
    return None


def period_date(year: int, period: str, freq: str | None, *, end: bool) -> date | None:
    """Fecha (primer día del periodo) de `year/period` para una serie de frecuencia `freq`.

    Un promedio anual como extremo se traduce al último (o primer) periodo del año.
    """
    info = period_info(period)
    if info is None:
        return None
    kind, month = info
    if kind == _ANNUAL and freq in ("M", "Q"):
        month = (12 if freq == "M" else 10) if end else 1
    return date(year, month, 1)


def obs_date(year: int, period: str, freq: str | None) -> date | None:
    """Fecha de una observación si su periodo casa con la frecuencia de la serie; si no, None."""
    info = period_info(period)
    if info is None or freq is None:
        return None
    kind, month = info
    if kind == freq or (freq == "A" and kind == _ANNUAL):
        return date(year, month, 1)
    return None


# --- tablas de códigos y geografía -------------------------------------------------------------


def parse_code_table(lines: list[list[str]], column: str) -> dict[str, str]:
    """código -> texto de una tabla `<s>.<dimensión>` (filas ya separadas y recortadas).

    La clave es la columna con el mismo nombre que en `.series` (o la primera); el texto, la
    primera columna `*_text|_name|_title`. Algunas tablas antiguas no traen cabecera: entonces
    clave = columna 0 y texto = columna 1.
    """
    if not lines:
        return {}
    head = lines[0]
    has_header = column in head or any(h.endswith(_CODE_SUFFIX) for h in head)
    if has_header:
        key = head.index(column) if column in head else 0
        text = next((i for i, h in enumerate(head) if i != key and _TEXT_COL_RE.search(h)), key + 1)
        rows = lines[1:]
    else:
        key, text, rows = 0, 1, lines
    return {r[key]: r[text] for r in rows if len(r) > max(key, text)}


def geo_classify(codes: dict[str, str], maps: dict[str, dict[str, str]]) -> tuple[str, str]:
    """(geo_level, geo_detail) de una serie según sus dimensiones geográficas.

    `codes` = columna geográfica -> código de la serie. Criterio:
    - Sin dimensión geográfica → "nacional" (BLS publica totales EE. UU. por defecto) con
      detalle "sin_dimension_geografica".
    - Las dimensiones cuya tabla tiene una sola fila no informan (p. ej. "todas las áreas").
    - Con dimensiones informativas: "internacional" si una dimensión de país no es EE. UU.;
      "nacional" si al menos una es de nivel país (`_NATIONAL_RE`) y el resto es nacional o
      neutra ("statewide"); "regional" en otro caso.
    - Con dimensiones geográficas sin tabla de códigos: se ignoran si hay otras con tabla; si
      ninguna tiene tabla, "regional" (no se puede verificar el nivel nacional).
    """
    if not codes:
        return "nacional", "sin_dimension_geografica"
    detail = ";".join(f"{c}={v}" for c, v in codes.items())
    mapped = [c for c in codes if c in maps]
    if not mapped:
        return "regional", detail + ";sin_tabla"
    informative = [c for c in mapped if len(maps[c]) > 1]
    national = neutral = 0
    for col in informative:
        text = maps[col].get(codes[col], "")
        is_nat = bool(_NATIONAL_RE.search(text))
        if _COUNTRY_COL_RE.search(col) and not is_nat:
            return "internacional", detail
        if is_nat:
            national += 1
        elif _NEUTRAL_GEO_RE.match(text):
            neutral += 1
    if not informative or (national and national + neutral == len(informative)):
        return "nacional", detail
    return "regional", detail


def repair_header(header: list[str], first: list[str] | None) -> tuple[list[str], list[list[str]]]:
    """Cabecera corregida y filas iniciales, para dos defectos vistos en `.series` antiguos
    (sin nombrar encuestas):

    - cabecera sin salto de línea antes de la primera fila (la última columna de la cabecera
      viene pegada al primer series_id): se separa por la columna final `end_period`;
    - columnas sin nombre en la cabecera que las filas no traen: se eliminan.
    """
    rest = [first] if first is not None else []
    for i, h in enumerate(header):
        if h != _END_PERIOD_COL and h.startswith(_END_PERIOD_COL):
            glued = [h.removeprefix(_END_PERIOD_COL), *header[i + 1 :]]
            return [*header[:i], _END_PERIOD_COL], [glued, *rest]
    if first is not None and "" in header:
        named = [h for h in header if h]
        if len(first) == len(named):
            return named, rest
    return header, rest


def clean_title(title: str, seasonal_map: dict[str, str]) -> str:
    """Quita del título el texto del ajuste estacional (de la tabla `<s>.seasonal`).

    Así la versión SA y la NSA de una variable comparten título y la regla de variantes del
    embudo se queda con una. Si el resultado queda vacío, devuelve el título original.
    """
    out = title
    for text in sorted(seasonal_map.values(), key=len, reverse=True):
        if text:
            out = re.sub(re.escape(text), "", out, flags=re.IGNORECASE)
    for code in seasonal_map:
        out = re.sub(rf"\(\s*{re.escape(code)}\s*\)", "", out)
    out = re.sub(r"\s*,\s*(?:,\s*)+", ", ", out)
    out = re.sub(r"\s+", " ", out).strip(" ,;:-")
    return out or title


def to_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _parse_listing(html: str) -> list[dict[str, Any]]:
    out = []
    for d, t, size, href, name in _LISTING_RE.findall(html):
        is_dir = not size.isdigit()
        hhmm = " ".join(t.split())
        out.append(
            {
                "name": name.strip().rstrip("/"),
                "href": href,
                "is_dir": is_dir,
                "size": None if is_dir else int(size),
                "mtime": f"{d} {hhmm}",
            }
        )
    return out


def _parse_mtime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%m/%d/%Y %I:%M %p").replace(tzinfo=UTC)
    except ValueError:
        return None


# --- adaptador ---------------------------------------------------------------------------------


class BlsAdapter:
    """`SourceAdapter` de BLS sobre los archivos masivos (ver docstring del módulo)."""

    source = SOURCE

    def __init__(
        self,
        cfg: dict[str, Any],
        paths: SourcePaths,
        logger: logging.Logger | None = None,
        *,
        user_agent: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        scfg = cfg["sources"][SOURCE]
        self.paths = paths
        self.log = logger or logging.getLogger(__name__)
        self.client = ApiClient(
            scfg.get("base_url", DEFAULT_BASE_URL),
            {},
            requests_per_minute=scfg["requests_per_minute"],
            max_retries=scfg["max_retries"],
            timeout_s=scfg["timeout_s"],
            backoff_base_s=scfg["backoff_base_s"],
            headers={"User-Agent": user_agent},
            transport=transport,
            clock=clock,
            sleep=sleep,
            logger=self.log,
        )
        self.max_map_bytes = scfg.get("max_map_bytes", MAX_MAP_BYTES)
        self._units: list[Unit] | None = None

    # --- unidades ------------------------------------------------------------------------------

    def reset_cache(self) -> None:
        """Olvida el listado de unidades (se cachea por instancia: crawl y download lo piden)."""
        self._units = None

    def list_units(self) -> list[Unit]:
        if self._units is not None:
            return self._units
        index = _parse_listing(self._text(""))
        names = self._survey_names(index)
        units = []
        for entry in index:
            if not entry["is_dir"]:
                continue
            code = entry["name"]
            files = {
                f["name"]: f for f in _parse_listing(self._text(f"{code}/")) if not f["is_dir"]
            }
            series_file = f"{code}.series"
            if series_file not in files:
                self.log.info(
                    "directorio sin .series: no es una encuesta",
                    extra={"fields": {"source": SOURCE, "dir": code}},
                )
                continue
            headers = self.client.head(f"{code}/{series_file}")
            etag = headers.get("etag")
            last_mod = headers.get("last-modified")
            listed = files[series_file]
            version = etag or last_mod or f"{listed['size']}@{listed['mtime']}"
            units.append(
                Unit(
                    code,
                    version,
                    {
                        "name": names.get(code, code),
                        "files": files,
                        "last_modified": last_mod,
                    },
                )
            )
        self._units = units
        return units

    def _survey_names(self, index: list[dict[str, Any]]) -> dict[str, str]:
        """Nombre de cada encuesta desde el `.txt` de resumen del índice (si existe)."""
        dirs = {e["name"].lower() for e in index if e["is_dir"]}
        out: dict[str, str] = {}
        for entry in index:
            if entry["is_dir"] or not entry["name"].endswith(".txt"):
                continue
            for line in self._text(entry["name"]).splitlines():
                m = _OVERVIEW_RE.match(line)
                if m and m.group(1).lower() in dirs and m.group(1).isupper():
                    out.setdefault(m.group(1).lower(), m.group(2))
        return out

    # --- catálogo ------------------------------------------------------------------------------

    def catalog_rows(self, unit: Unit) -> Iterator[dict[str, Any]]:
        """Filas del catálogo neutral, leyendo `<s>.series` en streaming."""
        code = unit.unit_id
        files: dict[str, dict[str, Any]] = unit.extra["files"]
        last_updated = self._last_updated(unit)
        stats: Counter[str] = Counter()
        with self.client.stream(f"{code}/{code}.series") as resp:
            lines = _tsv(resp.iter_lines())
            header = next(lines, None)
            if header is None:
                return
            original = header
            header, leading = repair_header(header, next(lines, None))
            if header != original:
                stats["cabecera_reparada"] += 1
            lines = itertools.chain(leading, lines)
            idx = {h: i for i, h in enumerate(header)}
            code_cols = [
                h for h in header if h.endswith(_CODE_SUFFIX) and not h.startswith(_FOOTNOTE_PREFIX)
            ]
            maps = self._load_maps(code, files, [*code_cols, _SEASONAL_COL])
            seasonal_map = maps.get(_SEASONAL_COL, {})
            geo_cols = [c for c in code_cols if _GEO_COL_RE.search(c[: -len(_CODE_SUFFIX)])]
            units_col = next((c for c in code_cols if _UNITS_COL_RE.match(c) and c in maps), None)
            title_cols = [c for c in code_cols if c in maps]
            n_cols = len(header)
            for row in lines:
                if len(row) > n_cols and not any(row[n_cols:]):
                    row = row[:n_cols]
                if len(row) != n_cols:
                    stats["filas_malformadas"] += 1
                    continue
                try:
                    yield self._catalog_row(
                        row, idx, unit, maps, seasonal_map, geo_cols, units_col, title_cols,
                        last_updated, stats,
                    )  # fmt: skip
                except ValueError:
                    stats["filas_malformadas"] += 1
        self.log.info(
            "catálogo de encuesta leído",
            extra={"fields": {"source": SOURCE, "unit": code, **dict(stats)}},
        )

    def _catalog_row(
        self,
        row: list[str],
        idx: dict[str, int],
        unit: Unit,
        maps: dict[str, dict[str, str]],
        seasonal_map: dict[str, str],
        geo_cols: list[str],
        units_col: str | None,
        title_cols: list[str],
        last_updated: datetime | None,
        stats: Counter[str],
    ) -> dict[str, Any]:
        def get(col: str) -> str:
            return row[idx[col]] if col in idx else ""

        sid = get("series_id")
        if not sid:
            raise ValueError("series_id vacío")
        begin_p, end_p = get("begin_period"), get("end_period")
        begin_y, end_y = int(get("begin_year")), int(get("end_year"))
        freq = series_frequency(begin_p, end_p)
        stats[f"frecuencia_{freq}"] += 1
        title = get(_TITLE_COL)
        if title:
            title = clean_title(title, seasonal_map)
        else:
            parts = [maps[c].get(get(c), "") for c in title_cols]
            title = ", ".join(p for p in parts if p) or sid
        geo_level, geo_detail = geo_classify({c: get(c) for c in geo_cols}, maps)
        return {
            "series_id": sid,
            "title": title,
            "frequency": freq if freq in ("M", "Q", "A") else None,
            "frequency_raw": end_p,
            "units": maps[units_col].get(get(units_col)) if units_col else None,
            "seasonal_adjustment": _SEASONAL_CODES.get(get(_SEASONAL_COL)),
            "observation_start": period_date(begin_y, begin_p, freq, end=False),
            "observation_end": period_date(end_y, end_p, freq, end=True),
            "last_updated": last_updated,
            "discontinued": False,
            "copyrighted": False,  # dominio público federal
            "geo_level": geo_level,
            "geo_detail": geo_detail,
            "category_path": unit.extra.get("name") or unit.unit_id,
        }

    def _load_maps(
        self, code: str, files: dict[str, dict[str, Any]], columns: list[str]
    ) -> dict[str, dict[str, str]]:
        """Tablas de códigos de las columnas: `<s>.<dim>` con dim = columna sin `_code`,
        comparando sin guiones bajos (data_type_code ↔ datatype)."""
        by_stem = {
            name.split(".", 1)[1].replace("_", ""): name
            for name in files
            if name.count(".") == 1 and name.startswith(code + ".")
        }
        maps: dict[str, dict[str, str]] = {}
        for col in columns:
            stem = col.removesuffix(_CODE_SUFFIX).replace("_", "")
            name = by_stem.get(stem)
            if name is None or name.endswith(".series"):
                continue
            size = files[name]["size"] or 0
            if size > self.max_map_bytes:
                self.log.warning(
                    "tabla de códigos omitida por tamaño",
                    extra={"fields": {"source": SOURCE, "file": name, "bytes": size}},
                )
                continue
            lines = list(_tsv(self._text(f"{code}/{name}").splitlines()))
            maps[col] = parse_code_table(lines, col)
        return maps

    def _last_updated(self, unit: Unit) -> datetime | None:
        value = unit.extra.get("last_modified")
        if value:
            try:
                return parsedate_to_datetime(value).astimezone(UTC).replace(microsecond=0)
            except (TypeError, ValueError):
                pass
        listed = unit.extra["files"].get(f"{unit.unit_id}.series")
        return _parse_mtime(listed["mtime"]) if listed else None

    # --- valores -------------------------------------------------------------------------------

    def _data_files(self, unit: Unit) -> list[dict[str, Any]]:
        prefix = f"{unit.unit_id}.data."
        files = [f for f in unit.extra["files"].values() if f["name"].startswith(prefix)]
        return sorted(files, key=lambda f: (-(f["size"] or 0), f["name"]))

    def values_version(self, unit: Unit) -> str | None:
        """Huella de los archivos `.data.*` (nombre, tamaño y fecha del listado).

        Equivale a su Last-Modified (con precisión de minuto) más el tamaño, sin un HEAD por
        archivo (hay encuestas con más de 80 archivos de datos).
        """
        files = self._data_files(unit)
        if not files:
            return None
        blob = "|".join(f"{f['name']}:{f['size']}:{f['mtime']}" for f in files)
        return hashlib.sha1(blob.encode()).hexdigest()[:20]

    def fetch_values(self, unit: Unit, series_ids: set[str]) -> Iterator[dict[str, Any]]:
        """Valores de las series pedidas, recorriendo los `.data.*` de mayor a menor tamaño.

        Se para cuando todas las series pedidas ya mostraron su primera y su última observación
        del catálogo (el archivo más grande suele cubrirlo todo; otros son subconjuntos, p. ej.
        solo lo reciente). La capa común deduplica (series_id, date) entre archivos.
        """
        meta = self._catalog_meta(series_ids)
        pending = {
            sid: {m["observation_start"], m["observation_end"]}
            for sid, m in meta.items()
            if m["observation_start"] and m["observation_end"]
        }
        incomplete = set(series_ids) - set(pending)
        stats: Counter[str] = Counter()
        for f in self._data_files(unit):
            if not pending and not incomplete:
                break
            stats["archivos"] += 1
            with self.client.stream(f"{unit.unit_id}/{f['name']}") as resp:
                lines = _tsv(resp.iter_lines())
                next(lines, None)  # cabecera
                for row in lines:
                    if len(row) < 4:
                        stats["filas_malformadas"] += 1
                        continue
                    sid = row[0]
                    m = meta.get(sid)
                    if m is None:
                        continue
                    try:
                        year = int(row[1])
                    except ValueError:
                        stats["filas_malformadas"] += 1
                        continue
                    d = obs_date(year, row[2], m["frequency"])
                    if d is None:
                        stats["periodo_descartado"] += 1
                        continue
                    value = to_float(row[3])
                    if value is None:
                        stats["valor_no_numerico"] += 1
                    need = pending.get(sid)
                    if need is not None:
                        need.discard(d)
                        if not need:
                            del pending[sid]
                    stats["filas"] += 1
                    yield {
                        "series_id": sid,
                        "date": d,
                        "value": value,
                        "frequency": m["frequency"],
                        "units": m["units"],
                        "seasonal_adjustment": m["seasonal_adjustment"],
                    }
        stats["series_sin_extremos"] = len(pending)
        self.log.info(
            "valores de encuesta leídos",
            extra={"fields": {"source": SOURCE, "unit": unit.unit_id, **dict(stats)}},
        )

    def _catalog_meta(self, series_ids: set[str]) -> dict[str, dict[str, Any]]:
        cols = ["series_id", "frequency", "units", "seasonal_adjustment"]
        t = read_table(
            SOURCE_CATALOG,
            self.paths.catalog,
            source=SOURCE,
            where="list_contains(?, series_id)",
            params=[sorted(series_ids)],
        )
        keep = [*cols, "observation_start", "observation_end"]
        return {r["series_id"]: r for r in t.select(keep).to_pylist()}

    # --- E/S -----------------------------------------------------------------------------------

    def _text(self, endpoint: str) -> str:
        with self.client.stream(endpoint) as resp:
            resp.read()
            return resp.text

    def close(self) -> None:
        self.client.close()


def _tsv(lines: Iterator[str] | list[str]) -> Iterator[list[str]]:
    for line in lines:
        line = line.rstrip("\r\n")
        if line.strip():
            yield [c.strip() for c in line.split("\t")]


def user_agent_from_env(cfg: dict[str, Any]) -> str:
    """User-Agent desde la variable de entorno configurada; error claro sin mostrar valores."""
    name = cfg["sources"]["user_agent_env"]
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Falta la variable de entorno {name}: download.bls.gov exige un User-Agent con "
            "nombre y correo de contacto (ver .env.example)."
        )
    return value


def make_adapter(cfg: dict[str, Any], paths: SourcePaths, logger: logging.Logger) -> BlsAdapter:
    return BlsAdapter(cfg, paths, logger, user_agent=user_agent_from_env(cfg))


def main(argv: list[str] | None = None) -> int:
    return common.main(argv, SOURCE, make_adapter)


if __name__ == "__main__":
    sys.exit(main())
