"""Base común de los adaptadores de mercado por NAICS de Census (CBP, BDS, Censo Económico).

Estas fuentes son anuales y cortas: son **métricas de mercado para las fichas, no entran a los
detectores** (ADR 0024). Por eso `config.yaml` las deja fuera de `detect.sources` y
`sources.<fuente>.ignore_staleness` las exime de la regla «inactiva» del embudo: un dato anual
publicado con años de rezago sigue siendo la cifra vigente.

Una unidad por fuente (todas las vintages juntas, porque una serie NAICS cruza vintages). Se
recorre TODO el árbol NAICS que devuelve la API (`NAICS*=*`); ningún código se escribe a mano
(principio 1). Variables y vintages se descubren de `data.json` y `variables.json`: el nombre de la
variable NAICS cambia por vintage (NAICS1997, NAICS2002, NAICS2012, NAICS2017, NAICS2022).

`MARKET_NAICS_PREFIX` (entorno) acota la corrida a los NAICS que empiezan por ese prefijo
(muestras y pruebas); sin definir se descarga todo.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from src.ingest.common import SourcePaths, Unit
from src.ingest.http import ApiClient, ApiError, RateLimiter

AGENCY = "U.S. Census Bureau"
BASE_URL = "https://api.census.gov"
CATALOG_ENDPOINT = "/data.json"
KEY_ENV = "CENSUS_API_KEY"
PREFIX_ENV = "MARKET_NAICS_PREFIX"
NAICS_VAR_RE = re.compile(r"^NAICS(?:\d{4})?$")
NAICS_CODE_RE = re.compile(r"^(?:\d{2}-\d{2}|\d{2,6})$")
GEO_PARAM = {"for": "us:*"}
MARKET_SOURCES = ("cbp", "bds", "econcensus")


@dataclass
class MSeries:
    title: str
    units: str
    category_path: str
    obs: dict[date, float] = field(default_factory=dict)


def units_of(var_name: str, label: str, ptype: str | None) -> str:
    """Unidades según el nombre y la etiqueta oficiales de la variable (sin listas de temas)."""
    if re.search(r"\$\s*1,000", label):
        return "Thousands of dollars"
    if re.search(r"\brate\b|percent|_PCT$", f"{label} {var_name}", re.IGNORECASE):
        return "Percent"
    return "Number"


def clean_code(raw: str) -> str | None:
    """Código NAICS sin relleno (`'62    '` -> `'62'`); None si no es un código de industria."""
    code = (raw or "").strip().rstrip("-/ ")
    if raw and re.fullmatch(r"\d{2}-\d{2}", raw.strip()):
        code = raw.strip()
    return code if code == "00" or NAICS_CODE_RE.match(code) else None


def to_float(value: Any) -> float | None:
    """Valor numérico o None (suprimidos y códigos de anotación negativos no son datos)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


class NaicsAdapter:
    """Contrato `SourceAdapter` común; cada fuente implementa `_dataset_paths` y `_load`."""

    source: str = ""

    def __init__(
        self,
        client: ApiClient,
        cfg: dict[str, Any],
        paths: SourcePaths,
        *,
        today: Callable[[], date] = date.today,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.paths = paths
        self.scfg = cfg["sources"][self.source]
        self.today = today
        self.log = logger or logging.getLogger(f"ingest.{self.source}")
        self.prefix = os.environ.get(PREFIX_ENV, "").strip()
        self._datasets: list[dict[str, Any]] | None = None
        self._loaded: dict[str, MSeries] | None = None

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
    ) -> NaicsAdapter:
        scfg = cfg["sources"][cls.source]
        client = ApiClient(
            BASE_URL,
            {"key": api_key} if api_key else {},
            requests_per_minute=scfg["requests_per_minute"],
            max_retries=scfg["max_retries"],
            timeout_s=scfg["timeout_s"],
            backoff_base_s=scfg["backoff_base_s"],
            raw_dir=paths.raw / cls.source,
            transport=transport,
            logger=logger,
            sleep=sleep,
            limiter=RateLimiter(scfg["requests_per_minute"], sleep=sleep),
        )
        return cls(client, cfg, paths, today=today, logger=logger)

    # --- a implementar -------------------------------------------------------------------------

    def _dataset_paths(self) -> list[tuple[str, str, str]]:
        """(ruta, vintage, `modified`) de los datasets de la fuente en data.json, ordenadas."""
        raise NotImplementedError

    def _load(self) -> dict[str, MSeries]:
        raise NotImplementedError

    # --- contrato ------------------------------------------------------------------------------

    def _version(self) -> str:
        blob = json.dumps(
            [self._dataset_paths(), self.prefix, self.scfg.get("measures")], sort_keys=True
        )
        return hashlib.sha1(blob.encode()).hexdigest()[:16]

    def list_units(self) -> list[Unit]:
        return [Unit(self.source, catalog_version=self._version())]

    def values_version(self, unit: Unit) -> str | None:
        return self._version()

    def catalog_rows(self, unit: Unit) -> Iterator[dict[str, Any]]:
        for sid, s in sorted(self._series().items()):
            dates = sorted(s.obs)
            if not dates:
                continue
            yield dict(
                series_id=sid,
                title=s.title,
                frequency="A",
                frequency_raw="A",
                units=s.units,
                seasonal_adjustment=None,
                observation_start=dates[0],
                observation_end=dates[-1],
                last_updated=datetime(self.today().year, self.today().month, 1, tzinfo=UTC),
                discontinued=False,
                copyrighted=False,
                geo_level="nacional",
                geo_detail="us",
                category_path=s.category_path,
            )

    def fetch_values(self, unit: Unit, series_ids: set[str]) -> Iterator[dict[str, Any]]:
        series = self._series()
        for sid in sorted(series_ids & series.keys()):
            s = series[sid]
            for d, v in sorted(s.obs.items()):
                yield {
                    "series_id": sid,
                    "date": d,
                    "value": v,
                    "frequency": "A",
                    "units": s.units,
                    "seasonal_adjustment": None,
                }

    def close(self) -> None:
        self.client.close()

    # --- utilidades para las subclases ----------------------------------------------------------

    def _series(self) -> dict[str, MSeries]:
        if self._loaded is None:
            self._loaded = self._load()
        return self._loaded

    def _catalog(self) -> list[dict[str, Any]]:
        if self._datasets is None:
            data = self.client.get_json(CATALOG_ENDPOINT, {})
            self._datasets = list(data.get("dataset", []))
        return self._datasets

    def _find_datasets(self, name: str) -> list[dict[str, Any]]:
        """Datasets cuyo `c_dataset` termina en `name` (excluye los de otro prefijo)."""
        return [d for d in self._catalog() if (d.get("c_dataset") or [None])[-1] == name]

    @staticmethod
    def _path(ds: dict[str, Any]) -> str:
        url = (ds.get("distribution") or [{}])[0].get("accessURL") or ""
        return url.split("api.census.gov", 1)[-1]

    def _variables(self, path: str) -> dict[str, Any]:
        return self.client.get_json(f"{path}/variables.json", {}, use_cache=False)["variables"]

    def _naics_var(self, variables: dict[str, Any]) -> str | None:
        """Nombre de la variable NAICS del dataset (None en las vintages con SIC)."""
        names = sorted(k for k in variables if NAICS_VAR_RE.match(k))
        return names[-1] if names else None

    def _query(self, path: str, get: list[str], where: dict[str, str]) -> list[dict[str, str]]:
        """Filas de la API como dicts; un error de la API se propaga (nada se omite en silencio)."""
        params = {"get": ",".join(get), **GEO_PARAM, **where}
        data = self.client.get_json(path, params)
        if not isinstance(data, list) or not data:
            raise ApiError(f"respuesta vacía o inesperada de {path}")
        head = data[0]
        return [dict(zip(head, row, strict=False)) for row in data[1:]]

    def _wild(self) -> str:
        return f"{self.prefix}*" if self.prefix else "*"

    def _dataset_list(self, name: str, min_vintage: int = 0) -> list[tuple[str, str, str]]:
        out = []
        for ds in self._find_datasets(name):
            vintage = str(ds.get("c_vintage") or "")
            if vintage.isdigit() and int(vintage) >= min_vintage:
                out.append((self._path(ds), vintage, str(ds.get("modified") or "")))
        return sorted(out, key=lambda t: t[1])

    @staticmethod
    def _add(
        series: dict[str, MSeries], sid: str, title: str, units: str, cat: str, year: str, v: Any
    ) -> None:
        value = to_float(v)
        if value is None or not year.isdigit():
            return
        s = series.setdefault(sid, MSeries(title, units, cat))
        s.obs[date(int(year), 1, 1)] = value

    def _need(self, variables: dict[str, Any], names: list[str], path: str) -> None:
        missing = [n for n in names if n not in variables]
        if missing:
            raise ApiError(f"{path}: la API ya no declara las variables {missing}")


def make_adapter_for(cls: type[NaicsAdapter]) -> Callable[..., NaicsAdapter]:
    def make(cfg: dict[str, Any], paths: SourcePaths, logger: logging.Logger) -> NaicsAdapter:
        api_key = os.environ.get(KEY_ENV)
        if not api_key:
            print(f"{KEY_ENV} no está definida (ver .env.example)", file=sys.stderr)
            raise SystemExit(2)
        return cls.from_config(cfg, paths, api_key, logger)

    return make
