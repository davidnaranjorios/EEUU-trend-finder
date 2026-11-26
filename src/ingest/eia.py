"""Adaptador de la U.S. Energy Information Administration (EIA), archivos masivos (ADR 0034).

Fuente: `https://www.eia.gov/opendata/bulk/<DATASET>.zip` (sin clave). Cada zip trae un `.txt` con
un JSON por línea; las líneas con `series_id` son series (`data` = [[periodo, valor], ...]) y las
demás son categorías (se ignoran). Una unidad = un archivo masivo (`bulk_datasets` en config.yaml).

- Periodos: `YYYYMM` (M), `YYYYQn` (Q), `YYYY` (A); la fecha es el primer día del periodo.
- Se omiten las series de planta o instalación (traen `lat`/`lon`): son puntos, no agregados, y
  son ~85 % de las líneas. Se cuentan en el log. El resto se cataloga completo.
- Geografía: `iso3166 == USA` nacional; `USA-XX` regional (estado o región); sin código: sin
  geografía. La capa común descarga solo `download.groups` (nacional por defecto).
- Sin NAICS en los metadatos de EIA: el anclaje lo resuelve `src/rank/naics.py` con sus métodos
  neutrales (no se escribe ninguna correspondencia a mano).
- EIA republica algunas series en FRED: el origen común se declara en `config/data_origin.yaml`.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from src.ingest import common
from src.ingest.common import SourcePaths, Unit
from src.ingest.http import ApiClient

SOURCE = "eia"
# Agencia con el nombre que usa FRED en `release/sources` (ADR 0009).
AGENCY = "U.S. Energy Information Administration"
DEFAULT_BASE_URL = "https://www.eia.gov/opendata/bulk/"

_PERIOD_RE = re.compile(r"^(\d{4})(?:(\d{2})|Q([1-4]))?$")
FREQUENCIES = ("M", "Q", "A")


def period_date(period: str) -> date | None:
    """Primer día del periodo EIA: `202609`, `2026Q3` o `2026`; None si no se reconoce."""
    m = _PERIOD_RE.match(period or "")
    if not m:
        return None
    year, month, quarter = int(m.group(1)), m.group(2), m.group(3)
    try:
        if month:
            return date(year, int(month), 1)
        if quarter:
            return date(year, 3 * int(quarter) - 2, 1)
        return date(year, 1, 1)
    except ValueError:
        return None


def geo_of(iso: str | None) -> tuple[str, str | None]:
    """(nivel, detalle) desde `iso3166`: USA nacional, USA-XX regional, otro sin geografía."""
    if iso == "USA":
        return "nacional", "USA"
    if iso and iso.startswith("USA-"):
        return "regional", iso
    return "sin_geografia", iso or None


def is_facility(obj: dict[str, Any]) -> bool:
    """Serie de una instalación (planta, mina): lleva coordenadas. No es un agregado."""
    return "lat" in obj or "lon" in obj or "latlon" in obj


def parse_line(line: str) -> dict[str, Any] | None:
    """Serie de una línea del `.txt` (None: categoría, planta o línea malformada)."""
    if '"series_id"' not in line or '"latlon"' in line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or "series_id" not in obj or is_facility(obj):
        return None
    return obj


def _to_float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _last_updated(value: str | None) -> datetime | None:
    try:
        return (
            datetime.fromisoformat(value).astimezone(UTC).replace(microsecond=0) if value else None
        )
    except ValueError:
        return None


class EiaAdapter:
    """`SourceAdapter` de EIA sobre los archivos masivos (ver docstring del módulo)."""

    source = SOURCE

    def __init__(
        self,
        cfg: dict[str, Any],
        paths: SourcePaths,
        logger: logging.Logger | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        scfg = cfg["sources"][SOURCE]
        self.paths = paths
        self.log = logger or logging.getLogger(__name__)
        self.datasets: list[str] = list(scfg["bulk_datasets"])
        self.client = ApiClient(
            scfg.get("base_url", DEFAULT_BASE_URL),
            {},
            requests_per_minute=scfg["requests_per_minute"],
            max_retries=scfg["max_retries"],
            timeout_s=scfg["timeout_s"],
            backoff_base_s=scfg["backoff_base_s"],
            headers={"User-Agent": _user_agent(cfg)},
            transport=transport,
            clock=clock,
            sleep=sleep,
            logger=self.log,
        )
        self._units: list[Unit] | None = None

    def reset_cache(self) -> None:
        self._units = None

    def list_units(self) -> list[Unit]:
        if self._units is None:
            units = []
            for name in self.datasets:
                h = self.client.head(f"{name}.zip")
                version = h.get("etag") or h.get("last-modified") or h.get("content-length")
                lm = h.get("last-modified")
                units.append(Unit(name, version, {"last_modified": lm}))
            self._units = units
        return self._units

    # --- archivo masivo ------------------------------------------------------------------------

    def _zip_path(self, unit: Unit) -> Path:
        tag = hashlib.sha1((unit.catalog_version or "").encode()).hexdigest()[:10]
        return self.paths.raw / SOURCE / f"{unit.unit_id}-{tag}.zip"

    def _local_zip(self, unit: Unit) -> Path:
        """Zip de la versión actual (se baja una vez; versiones anteriores se borran)."""
        dest = self._zip_path(unit)
        if not dest.exists():
            for old in dest.parent.glob(f"{unit.unit_id}-*.zip"):
                old.unlink()
            n = self.client.download(f"{unit.unit_id}.zip", dest)
            self.log.info(
                "archivo masivo descargado",
                extra={"fields": {"source": SOURCE, "unit": unit.unit_id, "bytes": n}},
            )
        return dest

    def _series(self, unit: Unit, stats: Counter[str]) -> Iterator[dict[str, Any]]:
        with zipfile.ZipFile(self._local_zip(unit)) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".txt"))
            with zf.open(name) as raw:
                for line in io.TextIOWrapper(raw, encoding="utf-8"):
                    obj = parse_line(line)
                    if obj is None:
                        stats["omitidas_categoria_o_instalacion"] += 1
                        continue
                    yield obj

    # --- catálogo y valores ----------------------------------------------------------------------

    def catalog_rows(self, unit: Unit) -> Iterator[dict[str, Any]]:
        stats: Counter[str] = Counter()
        for obj in self._series(unit, stats):
            freq = obj.get("f")
            start = period_date(str(obj.get("start") or ""))
            end = period_date(str(obj.get("end") or ""))
            if freq not in FREQUENCIES or start is None or end is None:
                stats["sin_frecuencia_o_fechas"] += 1
                continue
            level, detail = geo_of(obj.get("iso3166"))
            stats[f"geo_{level}"] += 1
            yield {
                "series_id": obj["series_id"],
                "title": obj.get("name") or obj["series_id"],
                "frequency": freq,
                "frequency_raw": freq,
                "units": obj.get("units"),
                "seasonal_adjustment": "NSA",  # EIA publica sin ajuste estacional
                "observation_start": start,
                "observation_end": end,
                "last_updated": _last_updated(obj.get("last_updated")),
                "discontinued": False,
                "copyrighted": str(obj.get("copyright") or "None").strip().lower()
                not in ("none", ""),
                "geo_level": level,
                "geo_detail": detail,
                "category_path": ";".join(obj["series_id"].split(".")[:2]),
            }
        self.log.info(
            "catálogo leído",
            extra={"fields": {"source": SOURCE, "unit": unit.unit_id, **dict(stats)}},
        )

    def values_version(self, unit: Unit) -> str | None:
        return unit.catalog_version

    def fetch_values(self, unit: Unit, series_ids: set[str]) -> Iterator[dict[str, Any]]:
        stats: Counter[str] = Counter()
        for obj in self._series(unit, stats):
            sid = obj["series_id"]
            if sid not in series_ids:
                continue
            freq = obj.get("f")
            for period, value in obj.get("data") or []:
                d = period_date(str(period))
                if d is None:
                    stats["periodo_descartado"] += 1
                    continue
                stats["filas"] += 1
                yield {
                    "series_id": sid,
                    "date": d,
                    "value": _to_float(value),
                    "frequency": freq,
                    "units": obj.get("units"),
                    "seasonal_adjustment": "NSA",
                }
        self.log.info(
            "valores leídos",
            extra={"fields": {"source": SOURCE, "unit": unit.unit_id, **dict(stats)}},
        )

    def close(self) -> None:
        self.client.close()


def _user_agent(cfg: dict[str, Any]) -> str:
    """User-Agent con contacto si `SEC_USER_AGENT` está definida; si no, un nombre genérico."""
    return os.environ.get(cfg["sources"]["user_agent_env"], "").strip() or "trend-finder"


def make_adapter(cfg: dict[str, Any], paths: SourcePaths, logger: logging.Logger) -> EiaAdapter:
    return EiaAdapter(cfg, paths, logger)


def main(argv: list[str] | None = None) -> int:
    """`uv run python -m src.ingest.eia crawl|download|update|estimate`."""
    return common.main(argv, SOURCE, make_adapter)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["AGENCY", "SOURCE", "EiaAdapter", "main", "make_adapter"]
