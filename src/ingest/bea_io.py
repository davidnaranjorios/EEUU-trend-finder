"""Insumo-producto de BEA: tabla de uso (*Use*), nivel resumen (ADR 0032).

Dataset `InputOutput` de la API de BEA. Se recorre con el mismo cliente, límites y análisis de
`src/ingest/bea.py` pero solo se piden las tablas cuya descripción oficial coincide con
`sources.bea_io.table_regex` (hoy «The Use of Commodities by Industries - Summary», `TableID`
259, años 1997 en adelante). El nivel detalle (más de 400 industrias) no se publica por la API:
`GetParameterValues` de `TableID` solo ofrece Summary y Sector; el Sector se omite porque agrega
las mismas cifras.

Fuente aparte de `bea` (que también recorre `InputOutput` completo y alimenta los detectores):
`bea_io` es de mercado, no entra a los detectores (ADR 0024) y `src.market build` la convierte en
`market/io_flows`. `series_id = InputOutput:<tabla>:<fila>.<tipo>.<columna>.<tipo>:A`; fecha =
1 de enero del año; valores en millones de dólares corrientes.

Uso: `uv run python -m src.ingest.bea_io crawl [--full] | download | update | estimate`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from src.ingest import common
from src.ingest.bea import (
    BASE_URL,
    KEY_ENV,
    BeaAdapter,
    BeaApiClient,
    _as_list,
    param_values,
    results,
)
from src.ingest.common import SourcePaths, Unit

SOURCE = "bea_io"
__all__ = ["SOURCE", "BeaIoAdapter", "build_adapter", "main", "make_adapter"]


class BeaIoAdapter(BeaAdapter):
    source = SOURCE

    def __init__(
        self, *args: Any, dataset: str, table_regex: str, title_fields: list[str], **kw: Any
    ) -> None:
        super().__init__(*args, **kw)
        self.title_fields = title_fields
        self.dataset = dataset
        self.table_re = re.compile(table_regex)

    def list_units(self) -> list[Unit]:
        if self._units is not None:
            return self._units
        data = self._request("GETDATASETLIST")
        desc = ""
        for res in results(data or {}):
            for d in _as_list(res.get("Dataset")):
                if isinstance(d, dict) and d.get("DatasetName") == self.dataset:
                    desc = str(d.get("DatasetDescription", ""))
        plan = self._plan(self.dataset, desc)
        units: list[Unit] = []
        if plan is not None:
            values = param_values(
                self._request(
                    "GetParameterValues", DatasetName=self.dataset, ParameterName=plan.unit_param
                )
                or {}
            )
            signature = json.dumps([plan.unit_param, plan.params], sort_keys=True)
            for key, tdesc in values:
                if not self.table_re.search(tdesc):
                    continue
                version = hashlib.sha1(f"{signature}|{key}|{tdesc}".encode()).hexdigest()[:16]
                units.append(
                    Unit(
                        f"{self.dataset}:{key}",
                        version,
                        extra={"plan": plan, "table": key, "table_desc": tdesc},
                    )
                )
        self.log.info(
            "unidades de insumo-producto",
            extra={"fields": {"unidades": len(units), "regex": self.table_re.pattern}},
        )
        self._units = units
        return units


def build_adapter(
    scfg: dict[str, Any],
    raw_dir: Path | None,
    api_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Any = None,
    sleep: Any = None,
    logger: logging.Logger | None = None,
) -> BeaIoAdapter:
    extra = {k: v for k, v in (("clock", clock), ("sleep", sleep)) if v is not None}
    client = BeaApiClient(
        BASE_URL,
        {"UserID": api_key},
        requests_per_minute=scfg["requests_per_minute"],
        max_retries=scfg["max_retries"],
        timeout_s=scfg["timeout_s"],
        backoff_base_s=scfg["backoff_base_s"],
        raw_dir=raw_dir,
        transport=transport,
        logger=logger,
        **extra,
    )
    return BeaIoAdapter(
        client,
        api_key=api_key,
        logger=logger,
        max_retries=scfg["max_retries"],
        dataset=scfg["dataset"],
        table_regex=scfg["table_regex"],
        title_fields=scfg["title_fields"],
        **extra,
    )


def make_adapter(cfg: dict[str, Any], paths: SourcePaths, logger: logging.Logger) -> BeaIoAdapter:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise SystemExit(f"Falta {KEY_ENV} en el entorno (.env)")
    return build_adapter(cfg["sources"][SOURCE], paths.raw / SOURCE, key, logger=logger)


def main(argv: list[str] | None = None) -> int:
    return common.main(argv, SOURCE, make_adapter)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
