"""Fake de la API de BEA para el test de contrato (sin red).

Respuestas con el formato real de https://apps.bea.gov/api/data (grabado el 2026-11-03,
recortado): `BEAAPI.Request` con el UserID devuelto (como hace BEA), `Results.Dataset`,
`Results.Parameter`, `Results.ParamValue` con nombres de campo distintos por dataset,
`Results.Data` con TimePeriod 2024 / 2024Q3, DataValue con comas y "(NA)", y errores dentro de
un 200 (`Results.Error`).

- Dataset tipo NIPA (`DSA`): tabla `TA1` con 3 líneas (A y Q; dos con la misma descripción),
  tabla `TA2` con 3 series inactivas (fila de linaje agregada) y tabla `TA3` que responde con
  un error dentro de un 200.
- Dataset tipo Regional (`DSR`, con GeoFips y LineCode): tabla `RG1` con 2 líneas, nivel
  nacional y estatal (2 estados) y un condado que no se pide.
- Dataset sin parámetro de año (`META`): se omite.
- La primera solicitud GetData responde 429 con Retry-After (el reintento usa un sleep falso).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from src.ingest.bea import build_adapter

FAKE_KEY = "FAKE-KEY-0123-SECRET"
RETRY_AFTER_S = 7
TARGET = "DSA:TA1:AAA.1:Q"


def _quarters(y0: int, y1: int, q_last: int) -> list[str]:
    return [f"{y}Q{q}" for y in range(y0, y1 + 1) for q in range(1, 5) if y < y1 or q <= q_last]


class FakeBea:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.sent_429 = False
        self.sleeps: list[float] = []
        self.now = 0.0
        self.nipa: dict[str, list[dict[str, Any]]] = {"TA1": [], "TA2": []}
        for code, line, desc in (
            ("AAA", "1", "Output"),
            ("BBB", "2", "Goods"),
            ("CCC", "3", "Goods"),
        ):
            for i, tp in enumerate(_quarters(1990, 2026, 2)):
                self.nipa["TA1"].append(_nipa_row("TA1", code, line, desc, tp, 100 + i))
            for i, y in enumerate(range(1990, 2026)):
                self.nipa["TA1"].append(_nipa_row("TA1", code, line, desc, str(y), 1000 + i))
        for code, line in (("DDD", "1"), ("EEE", "2"), ("FFF", "3")):
            for y in range(1980, 2005):
                self.nipa["TA2"].append(_nipa_row("TA2", code, line, f"Vieja {line}", str(y), 5))
        self.regional: list[dict[str, Any]] = []
        for line in ("1", "2"):
            for fips, name in (
                ("00000", "United States"),
                ("01000", "Alabama"),
                ("02000", "Alaska"),
            ):
                for y in range(1969, 2026):
                    val = (
                        "(NA)" if (fips == "02000" and y < 1990) else f"{1000 * int(line) + y:,}.5"
                    )
                    self.regional.append(
                        {
                            "Code": f"RG1-{line}",
                            "GeoFips": fips,
                            "GeoName": name,
                            "TimePeriod": str(y),
                            "Description": f"Línea {line} (miles) ",
                            "CL_UNIT": "Thousands of dollars",
                            "UNIT_MULT": "3",
                            "DataValue": val,
                        }
                    )

    # --- servidor ------------------------------------------------------------------------

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
        request_block = {
            "RequestParam": [
                {"ParameterName": k.upper(), "ParameterValue": v} for k, v in q.items()
            ]
        }
        return httpx.Response(
            200, content=json.dumps({"BEAAPI": {"Request": request_block, "Results": results}})
        )

    def _getdatasetlist(self, q: dict[str, str]) -> Any:
        return {
            "Dataset": [
                {"DatasetName": "DSA", "DatasetDescription": "Tablas tipo NIPA"},
                {"DatasetName": "DSR", "DatasetDescription": "Tablas tipo regional"},
                {"DatasetName": "META", "DatasetDescription": "Metadatos"},
            ]
        }

    def _getparameterlist(self, q: dict[str, str]) -> Any:
        ds = q["DatasetName"]
        if ds == "DSA":
            ps = [
                _param("Frequency", "1", ""),
                _param("ShowMillions", "0", ""),
                _param("TableID", "0", ""),
                _param("TableName", "0", ""),
                _param("Year", "1", "X"),
            ]
        elif ds == "DSR":
            ps = [
                _param("GeoFips", "1", None),
                _param("LineCode", "1", None),
                _param("TableName", "1", None),
                _param("Year", "0", None),
            ]
        else:
            ps = [_param("Dataset", "1", "")]
        return {"Parameter": ps}

    def _getparametervalues(self, q: dict[str, str]) -> Any:
        ds, p = q["DatasetName"], q["ParameterName"]
        if ds == "DSA" and p == "Frequency":
            return {
                "ParamValue": [
                    {"FrequencyID": "A", "Description": "Annual"},
                    {"FrequencyID": "Q", "Description": "Quarterly"},
                ]
            }
        if ds == "DSA" and p == "TableName":
            return {
                "ParamValue": [
                    {"TableName": t, "Description": f"Table {t}. Seasonally adjusted (A) (Q)"}
                    for t in ("TA1", "TA2", "TA3")
                ]
            }
        if ds == "DSR" and p == "TableName":
            return {"ParamValue": [{"Key": "RG1", "Desc": "Tabla regional anual"}]}
        return {"Error": {"APIErrorCode": "40", "APIErrorDescription": "parámetro inválido"}}

    def _getparametervaluesfiltered(self, q: dict[str, str]) -> Any:
        if q["TargetParameter"] == "LineCode":
            return {"ParamValue": [{"Key": k, "Desc": f"[RG1] Línea {k}"} for k in ("1", "2")]}
        geos = ("00000", "01000", "02000", "01001")
        return {"ParamValue": [{"Key": g, "Desc": g} for g in geos]}

    def _getdata(self, q: dict[str, str]) -> Any:
        if q["DatasetName"] == "DSR":
            geo, line = q["GeoFips"], q["LineCode"]
            rows = [
                {k: v for k, v in r.items() if k != "Description" or line == "ALL"}
                for r in self.regional
                if (geo == "STATE" or r["GeoFips"] == geo)
                and (line == "ALL" or r["Code"] == f"RG1-{line}")
            ]
            assert not (geo == "STATE" and line == "ALL")
            return {"Statistic": "Regional", "UnitOfMeasure": "See CL_UNIT", "Data": rows}
        table = q["TableName"]
        if table not in self.nipa:
            return {
                "Error": {
                    "APIErrorCode": "201",
                    "APIErrorDescription": "Error retrieving NIPA data.",
                    "ErrorDetail": {"Description": "Invalid TableName was requested."},
                }
            }
        freqs = set(q["Frequency"].split(","))
        rows = [r for r in self.nipa[table] if ("Q" if "Q" in r["TimePeriod"] else "A") in freqs]
        return {
            "Statistic": "NIPA Table",
            "Data": rows,
            "Notes": [
                {"NoteRef": table, "NoteText": f"Table {table} - LastRevised: October 29, 2026"}
            ],
        }


def _param(name: str, required: str, all_value: str | None) -> dict[str, Any]:
    p = {
        "ParameterName": name,
        "ParameterDataType": "string",
        "ParameterDescription": name,
        "ParameterIsRequiredFlag": required,
        "MultipleAcceptedFlag": "1",
    }
    if all_value is not None:
        p["AllValue"] = all_value
    return p


def _nipa_row(table: str, code: str, line: str, desc: str, tp: str, value: float) -> dict:
    return {
        "TableName": table,
        "SeriesCode": code,
        "LineNumber": line,
        "LineDescription": desc,
        "TimePeriod": tp,
        "METRIC_NAME": "Current Dollars",
        "CL_UNIT": "Level",
        "UNIT_MULT": "6",
        "DataValue": f"{value:,.1f}",
        "NoteRef": table,
    }


def make_adapter(tmp_path: Path, cfg: dict[str, Any]):
    fake = FakeBea()

    def clock() -> float:
        return fake.now

    def sleep(s: float) -> None:
        fake.sleeps.append(s)
        fake.now += s

    adapter = build_adapter(
        cfg["sources"]["bea"],
        tmp_path / "raw" / "bea",
        FAKE_KEY,
        transport=httpx.MockTransport(fake.handler),
        clock=clock,
        sleep=sleep,
    )
    adapter.fake = fake  # type: ignore[attr-defined]
    return adapter


def bump(adapter) -> None:
    """Publicación nueva: revisa el último trimestre de una serie nacional y añade el siguiente."""
    fake: FakeBea = adapter.fake
    rows = [r for r in fake.nipa["TA1"] if r["SeriesCode"] == "AAA" and "Q" in r["TimePeriod"]]
    last = rows[-1]
    last["DataValue"] = "9,999.0"
    y, q = int(last["TimePeriod"][:4]), int(last["TimePeriod"][-1])
    nxt = f"{y + (q == 4)}Q{q % 4 + 1}"
    fake.nipa["TA1"].append({**last, "TimePeriod": nxt, "DataValue": "12,345.6"})
    # Nueva corrida: la caché en memoria del adaptador dura una corrida.
    adapter._cache.clear()
