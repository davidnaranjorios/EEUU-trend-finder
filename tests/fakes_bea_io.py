"""Fake de la API de BEA para insumo-producto (contrato de fuentes; sin red).

Formato grabado el 2026-11-24 de `InputOutput` / TableID 259 (uso, nivel resumen), recortado a
3 mercancías, 3 industrias, 5 columnas de demanda final y 2 totales, años 1997-2023.
Tablas ofrecidas por `GetParameterValues`: 259 (Summary, la que se pide), 258 (Sector) y 262
(oferta), que no deben bajarse. Una celda sin `DataValue` (como la real de 2021).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from src.ingest.bea_io import build_adapter
from tests.fakes_bea import FAKE_KEY, RETRY_AFTER_S, _param

COMMODITIES = {"111CA": "Farms", "621": "Ambulatory health care services", "623": "Nursing care"}
INDUSTRIES = dict(COMMODITIES)
FINAL = {
    "F010": "Personal consumption expenditures",
    "F06C": "Federal national defense: Consumption expenditures",
    "F10C": "State and local government consumption expenditures",
    "F02E": "Private fixed investment in equipment",
    "F040": "Exports of goods and services",
}
TOTALS = {"T001": "Total Intermediate", "T019": "Total use of products"}
YEARS = list(range(1997, 2024))
TARGET = "InputOutput:259:623.Commodity.F010.Industry:A"


class FakeBeaIo:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.sent_429 = False
        self.rows: list[dict[str, str]] = []
        cols = {**INDUSTRIES, **FINAL, **TOTALS}
        for y in YEARS:
            for ri, (rc, rd) in enumerate(COMMODITIES.items()):
                for ci, (cc, cd) in enumerate(cols.items()):
                    row = {
                        "TableID": "259",
                        "Year": str(y),
                        "RowCode": rc,
                        "RowDescr": rd,
                        "RowType": "Commodity",
                        "ColCode": cc,
                        "ColDescr": cd,
                        "ColType": "Industry",
                        "DataValue": str(100 * (ri + 1) + 10 * ci + (y - 1997)),
                        "NoteRef": "259",
                    }
                    if (y, rc, cc) == (2021, "111CA", "F040"):
                        del row["DataValue"]  # celda sin valor
                    self.rows.append(row)

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        assert q.get("UserID") == FAKE_KEY
        self.calls.append(q)
        method = q["method"].lower()
        if method == "getdata" and not self.sent_429:
            self.sent_429 = True
            body = {"BEAAPI": {"Error": {"APIErrorCode": "7", "APIErrorDescription": "limit"}}}
            return httpx.Response(429, json=body, headers={"Retry-After": str(RETRY_AFTER_S)})
        results = getattr(self, "_" + method)(q)
        block = {
            "RequestParam": [
                {"ParameterName": k.upper(), "ParameterValue": v} for k, v in q.items()
            ]
        }
        return httpx.Response(
            200, content=json.dumps({"BEAAPI": {"Request": block, "Results": results}})
        )

    def _getdatasetlist(self, q: dict[str, str]) -> Any:
        return {"Dataset": [{"DatasetName": "InputOutput", "DatasetDescription": "Input-Output"}]}

    def _getparameterlist(self, q: dict[str, str]) -> Any:
        return {"Parameter": [_param("TableID", "1", ""), _param("Year", "1", "ALL")]}

    def _getparametervalues(self, q: dict[str, str]) -> Any:
        assert q["DatasetName"] == "InputOutput" and q["ParameterName"] == "TableID"
        return {
            "ParamValue": [
                {
                    "Key": "262",
                    "Desc": "The Domestic Supply of Commodities by Industries - Summary",
                },
                {"Key": "259", "Desc": "The Use of Commodities by Industries - Summary"},
                {"Key": "258", "Desc": "The Use of Commodities by Industries - Sector"},
            ]
        }

    def _getdata(self, q: dict[str, str]) -> Any:
        assert q["TableID"] == "259", "solo debe pedirse la tabla de uso, nivel resumen"
        return {
            "Statistic": "Input-Output Table",
            "UTCProductionTime": "2026-11-24T02:50:52.383",
            "Data": self.rows,
            "Notes": [
                {"NoteRef": "6009", "NoteText": "The Use of Commodities by Industries - Summary "}
            ],
        }


def make_adapter(tmp_path: Path, cfg: dict[str, Any]):
    fake = FakeBeaIo()
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(s: float) -> None:
        now[0] += s

    adapter = build_adapter(
        cfg["sources"]["bea_io"],
        tmp_path / "raw" / "bea_io",
        FAKE_KEY,
        transport=httpx.MockTransport(fake.handler),
        clock=clock,
        sleep=sleep,
    )
    adapter.fake = fake  # type: ignore[attr-defined]
    return adapter


def bump(adapter) -> None:
    """Publicación nueva: revisa 2023 de una serie y agrega 2024."""
    fake: FakeBeaIo = adapter.fake
    last = next(
        r
        for r in fake.rows
        if r["RowCode"] == "623" and r["ColCode"] == "F010" and r["Year"] == "2023"
    )
    last["DataValue"] = "99999"
    fake.rows.append({**last, "Year": "2024", "DataValue": "12345"})
    adapter._cache.clear()
