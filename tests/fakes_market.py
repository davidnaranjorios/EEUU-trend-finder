"""Fake de api.census.gov para CBP, BDS y el Censo Económico (httpx.MockTransport).

Forma real de la API: `data.json` con `c_dataset` y `c_vintage`, `variables.json` por dataset,
consultas con `get=` más los predicados (`for=us:*`, `NAICSxxxx=*`) que la API repite como columnas.
Datos pequeños con las rarezas que importan: nombre de variable NAICS distinto por vintage,
relleno de espacios en los códigos antiguos, una vintage sin NAICS (SIC) y una vintage anterior
a `min_vintage` que no debe pedirse.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from src.ingest.bds import BdsAdapter
from src.ingest.cbp import CbpAdapter
from src.ingest.common import SourcePaths
from src.ingest.econcensus import EconCensusAdapter
from src.ingest.naics_base import NaicsAdapter

FAKE_KEY = "fake-census-key-not-real"
TODAY = date(2026, 11, 13)
CODES = ("00", "62", "621", "6231")


def _var(label: str, ptype: str | None = "string", **kw: Any) -> dict[str, Any]:
    return {"label": label, "predicateType": ptype, **kw}


PREDICATES = {
    "for": _var("Census API FIPS 'for' clause", "fips-for", predicateOnly=True),
    "in": _var("Census API FIPS 'in' clause", "fips-in", predicateOnly=True),
}
CBP_MEASURES = {
    "ESTAB": _var("Number of establishments", "int"),
    "EMP": _var("Number of employees", "int"),
    "PAYANN": _var("Annual payroll ($1,000)", "int"),
}


def _naics_name(year: int) -> str:
    return "NAICS2007" if year < 2012 else "NAICS2012" if year < 2017 else "NAICS2017"


class FakeMarket:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.datasets: dict[str, dict[str, Any]] = {}
        # CBP: 1990 con SIC (sin NAICS), 2008..2023 con NAICS de nombre cambiante.
        self._add(
            "1990/cbp", ["cbp"], "1990", {**PREDICATES, **CBP_MEASURES, "SIC": _var("SIC")}, []
        )
        for y in range(2008, 2024):
            nv = _naics_name(y)
            pad = (lambda c: c.ljust(6)) if y < 2012 else (lambda c: c)
            rows = [
                {
                    "ESTAB": str(100 * (i + 1) + y - 2000),
                    "EMP": str(1000 * (i + 1) + y - 2000),
                    "PAYANN": str(10_000 * (i + 1) + y - 2000),
                    nv: pad(c),
                }
                for i, c in enumerate(CODES)
            ]
            rows.append(
                {"ESTAB": "5", "EMP": "(D)", "PAYANN": "-666666666", nv: "99X"}
            )  # no es NAICS
            self._add(
                f"{y}/cbp", ["cbp"], str(y), {**PREDICATES, **CBP_MEASURES, nv: _var("NAICS")}, rows
            )
        # BDS: todos los años en una consulta (YEAR=*).
        bds_vars = {
            **PREDICATES,
            "NAICS": _var("2017 NAICS Code"),
            "YEAR": _var("Year"),
            "FIRM": _var("Number of firms", "int"),
            "ESTAB": _var("Number of establishments", "int"),
            "ESTABS_ENTRY_RATE": _var(
                "Rate of establishments born during the last 12 months", "float"
            ),
            "ESTABS_EXIT_RATE": _var(
                "Rate of establishments exited during the last 12 months", "float"
            ),
        }
        bds_rows = [
            {
                "FIRM": str(50 * (i + 1) + y - 2000),
                "ESTAB": str(60 * (i + 1) + y - 2000),
                "ESTABS_ENTRY_RATE": f"{9 + i + (y - 2000) / 100:.3f}",
                "ESTABS_EXIT_RATE": f"{7 + i + (y - 2000) / 100:.3f}",
                "NAICS": c,
                "YEAR": str(y),
            }
            for y in range(2000, 2024)
            for i, c in enumerate(CODES)
        ]
        self._add("timeseries/bds", ["timeseries", "bds"], "", bds_vars, bds_rows)
        # Censo Económico: 2012 (no se debe pedir), 2017 y 2022.
        for y in (2012, 2017, 2022):
            nv = f"NAICS{y}"
            basic_vars = {
                **PREDICATES,
                nv: _var(f"{y} NAICS code"),
                "FIRM": _var("Number of firms", "int"),
                "ESTAB": _var("Number of establishments", "int"),
                "RCPTOT": _var("Sales, value of shipments, or revenue ($1,000)", "int"),
                "EMP": _var("Number of employees", "int"),
            }
            rows = [
                {
                    "FIRM": str(30 * (i + 1) + y - 2000),
                    "ESTAB": str(40 * (i + 1)),
                    "RCPTOT": str(1_000_000 * (i + 1) + y),
                    "EMP": "D" if c == "00" else str(500 * (i + 1)),
                    nv: c,
                }
                for i, c in enumerate(CODES)
            ]
            self._add(f"{y}/ecnbasic", ["ecnbasic"], str(y), basic_vars, rows)
            size_vars = {
                **PREDICATES,
                nv: _var(f"{y} NAICS code"),
                "CONCENFI": _var("Firm concentration code"),
                "CONCENFI_LABEL": _var("Meaning of Firm concentration code"),
                "VAL_PCT": _var(
                    "Sales, value of shipments, or revenue of largest firms (percent)", "float"
                ),
                "EMPSZFF": _var("Employment size of firms code"),
                "EMPSZFF_LABEL": _var("Meaning of Employment size of firms code"),
                "FIRM": _var("Number of firms", "int"),
            }
            rows = []
            for i, c in enumerate(CODES):
                for code, label, pct in (
                    ("001", "All firms", "100.0"),
                    ("604", "4 largest firms", f"{5 + i}.5"),
                    ("608", "8 largest firms", f"{8 + i}.5"),
                ):
                    rows.append({"CONCENFI": code, "CONCENFI_LABEL": label, "VAL_PCT": pct, nv: c})
                for code, label, n in (
                    ("001", "All firms", 100),
                    ("510", "Firms operated entire year with less than 5 employees", 10),
                    ("515", "Firms operated entire year with 5 to 9 employees", 20),
                    ("525", "Firms operated entire year with 20 to 49 employees", 30),
                    ("550", "Firms operated entire year with 500 employees or more", 40),
                ):
                    rows.append({"EMPSZFF": code, "EMPSZFF_LABEL": label, "FIRM": str(n), nv: c})
            self._add(f"{y}/ecnsize", ["ecnsize"], str(y), size_vars, rows)

    def _add(
        self, path: str, c_dataset: list[str], vintage: str, variables: dict, rows: list
    ) -> None:
        self.datasets[path] = {
            "c_dataset": c_dataset,
            "vintage": vintage,
            "modified": "2025-01-01",
            "vars": variables,
            "rows": rows,
        }

    def data_json(self) -> dict[str, Any]:
        out = []
        for path, d in self.datasets.items():
            ds: dict[str, Any] = {
                "c_dataset": d["c_dataset"],
                "modified": d["modified"],
                "distribution": [{"accessURL": f"https://api.census.gov/data/{path}"}],
            }
            if d["vintage"]:
                ds["c_vintage"] = int(d["vintage"])
            out.append(ds)
        out.append({"c_dataset": ["acs", "acs5"], "c_vintage": 2022, "distribution": []})
        return {"dataset": out}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/data.json":
            return httpx.Response(200, json=self.data_json())
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        for name, d in self.datasets.items():
            if path == f"/data/{name}/variables.json":
                return httpx.Response(200, json={"variables": d["vars"]})
            if path == f"/data/{name}":
                get = params["get"].split(",")
                preds = [k for k in params if k not in ("get", "key", "for")]
                need = [g for g in get if not g.endswith("_LABEL") or g in d["rows"][0]]
                rows = [r for r in d["rows"] if all(k in r for k in need)] if d["rows"] else []
                if not rows:
                    return httpx.Response(204)
                cols = [*get, *preds]
                body = [[*cols, "us"]] + [[r.get(c) for c in cols] + ["1"] for r in rows]
                return httpx.Response(200, json=body)
        return httpx.Response(404, text="not found")


def _make(cls: type[NaicsAdapter], tmp_path: Path, cfg: dict[str, Any]) -> NaicsAdapter:
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", cls.source)
    fake = FakeMarket()
    adapter = cls.from_config(
        cfg,
        paths,
        FAKE_KEY,
        transport=httpx.MockTransport(fake.handler),
        today=lambda: TODAY,
        sleep=lambda s: None,
    )
    adapter.fake = fake  # type: ignore[attr-defined]
    return adapter


def make_cbp(tmp_path: Path, cfg: dict[str, Any]) -> NaicsAdapter:
    return _make(CbpAdapter, tmp_path, cfg)


def make_bds(tmp_path: Path, cfg: dict[str, Any]) -> NaicsAdapter:
    return _make(BdsAdapter, tmp_path, cfg)


def make_econcensus(tmp_path: Path, cfg: dict[str, Any]) -> NaicsAdapter:
    return _make(EconCensusAdapter, tmp_path, cfg)


def bump_cbp(adapter: NaicsAdapter) -> None:
    """Vintage 2024 con un solo dato nuevo y una revisión de la 2023."""
    fake: FakeMarket = adapter.fake  # type: ignore[attr-defined]
    fake.datasets["2023/cbp"]["rows"][0]["ESTAB"] = "999"
    fake._add(
        "2024/cbp",
        ["cbp"],
        "2024",
        {**PREDICATES, **CBP_MEASURES, "NAICS2017": _var("NAICS")},
        [{"ESTAB": "7", "EMP": None, "PAYANN": None, "NAICS2017": "62"}],
    )
    adapter._datasets = None
    adapter._loaded = None


def bump_bds(adapter: NaicsAdapter) -> None:
    fake: FakeMarket = adapter.fake  # type: ignore[attr-defined]
    rows = fake.datasets["timeseries/bds"]["rows"]
    rows[0]["FIRM"] = "999"
    rows.append(
        {"FIRM": "1", "ESTAB": None, "ESTABS_ENTRY_RATE": None, "ESTABS_EXIT_RATE": None,
         "NAICS": "62", "YEAR": "2024"}
    )  # fmt: skip
    fake.datasets["timeseries/bds"]["modified"] = "2026-11-01"
    adapter._datasets = None
    adapter._loaded = None


def bump_econcensus(adapter: NaicsAdapter) -> None:
    fake: FakeMarket = adapter.fake  # type: ignore[attr-defined]
    fake.datasets["2022/ecnbasic"]["rows"][0]["FIRM"] = "999"
    fake._add(
        "2027/ecnbasic",
        ["ecnbasic"],
        "2027",
        {
            **PREDICATES,
            "NAICS2022": _var("2022 NAICS code"),
            "FIRM": _var("Number of firms", "int"),
            "ESTAB": _var("Number of establishments", "int"),
            "RCPTOT": _var("Sales", "int"),
            "EMP": _var("Employees", "int"),
        },
        [{"FIRM": "1", "ESTAB": None, "RCPTOT": None, "EMP": None, "NAICS2022": "62"}],
    )
    adapter._datasets = None
    adapter._loaded = None
