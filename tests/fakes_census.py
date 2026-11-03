"""Fake de api.census.gov (httpx.MockTransport) para el test de contrato y los unitarios.

Dos datasets timeseries con la forma real de la API:
- `timeseries/demo/largo`: formato "largo" tipo EITS (cell_value string, dimensiones de código,
  seasonally_adj yes/no, filas error_data=yes), mensual 2010-01..2026-09, nivel `us`.
- `timeseries/demo/ancho`: formato "ancho" (medidas int, una de error estándar, un año como
  medida, dimensión con etiqueta), anual hasta 2015: sus 3 series están inactivas y el
  crawl escribe una fila de linaje agregada.
Más un dataset no timeseries que debe ignorarse.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from src.ingest.census import CensusAdapter
from src.ingest.common import SourcePaths

FAKE_KEY = "fake-census-key-not-real"
TODAY = date(2026, 11, 3)


def _months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _dataset(path: str, title: str, modified: str, timeseries: bool = True) -> dict[str, Any]:
    url = f"https://api.census.gov/data/{path}"
    out = {
        "title": title,
        "modified": modified,
        "distribution": [{"accessURL": url}],
        "c_variablesLink": f"{url}/variables.json",
        "c_geographyLink": f"{url}/geography.json",
    }
    if timeseries:
        out["c_isTimeseries"] = True
    return out


def _var(label: str, ptype: str | None = "string", **kw: Any) -> dict[str, Any]:
    return {"label": label, "predicateType": ptype, **kw}


GEO_US = {"fips": [{"name": "us"}]}
PREDICATES = {
    "for": _var("Census API FIPS 'for' clause", "fips-for", predicateOnly=True),
    "in": _var("Census API FIPS 'in' clause", "fips-in", predicateOnly=True),
    "time": _var("ISO-8601 Date/Time value", "datetime", predicateOnly=True, required="true"),
}
LONG_VARS = {
    **PREDICATES,
    "cell_value": _var("data value", required="true"),
    "data_type_code": _var("item type", required="true"),
    "time_slot_id": _var("Time Slot", required="true"),
    "seasonally_adj": _var("Seasonally adjusted yes or no", required="true"),
    "category_code": _var("Industry list", required="true"),
    "error_data": _var("Error data yes or no"),
}
WIDE_VARS = {
    **PREDICATES,
    "NATION": _var("Nation", None),
    "GRP": _var("Group code", attributes="GRP_LABEL"),
    "GRP_LABEL": _var("Meaning of group code"),
    "TOT": _var("Total widgets", "int", attributes="TOT_F"),
    "TOT_F": _var("Flag for total widgets"),
    "TOT_S": _var("Relative standard error of total widgets", "int"),
    "PAY": _var("Payroll ($1,000)", "int"),
    "YEAR": _var("Year", "int"),
}


class FakeCensus:
    """Estado mutable de la API falsa y el manejador de MockTransport."""

    def __init__(self) -> None:
        self.modified = {"timeseries/demo/largo": "2017-02-23", "timeseries/demo/ancho": "2016"}
        self.requests: list[httpx.Request] = []
        months = _months(date(2010, 1, 1), date(2026, 9, 1))
        # (categoria, sa) -> {mes: valor}; categoría B tiene un valor suprimido "(S)".
        self.long: dict[tuple[str, str], dict[str, str]] = {}
        for ci, cat in enumerate(("A", "B")):
            for sa in ("yes", "no"):
                self.long[(cat, sa)] = {
                    t: f"{100 + ci * 50 + i + (0.5 if sa == 'no' else 0):.1f}"
                    for i, t in enumerate(months)
                }
        self.long[("B", "no")][months[3]] = "(S)"
        years = [str(y) for y in range(1990, 2016)]
        self.wide = {
            g: {y: (str(1000 * gi + i), str(10 * gi + i)) for i, y in enumerate(years)}
            for gi, g in enumerate(("1", "2", "3"), 1)
        }
        self.wide_labels = {"1": "Group one", "2": "Group two", "3": "Group three"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if path == "/data.json":
            return httpx.Response(200, json=self.data_json())
        for unit in self.modified:
            base = f"/data/{unit}"
            if path == f"{base}/variables.json":
                vars_ = LONG_VARS if unit.endswith("largo") else WIDE_VARS
                return httpx.Response(200, json={"variables": vars_})
            if path == f"{base}/geography.json":
                return httpx.Response(200, json=GEO_US)
            if path == base:
                return self.get_data(unit, params)
        return httpx.Response(404, text="not found")

    def data_json(self) -> dict[str, Any]:
        return {
            "dataset": [
                _dataset(
                    "timeseries/demo/largo", "Demo largo", self.modified["timeseries/demo/largo"]
                ),
                _dataset(
                    "timeseries/demo/ancho", "Demo ancho", self.modified["timeseries/demo/ancho"]
                ),
                _dataset("demo/2020/notime", "No timeseries", "2020", timeseries=False),
            ]
        }

    def get_data(self, unit: str, params: dict[str, str]) -> httpx.Response:
        get = params["get"].split(",")
        since = params["time"].removeprefix("from ").strip()
        vars_ = LONG_VARS if unit.endswith("largo") else WIDE_VARS
        unknown = [g for g in get if g not in vars_]
        missing = [v for v, d in vars_.items() if d.get("required") == "true" and v not in get]
        if unknown or (missing and missing != ["time"]):
            return httpx.Response(400, text=f"error: unknown/missing variable {unknown or missing}")
        rows: list[dict[str, str]] = []
        if unit.endswith("largo"):
            for (cat, sa), vals in self.long.items():
                for t, v in vals.items():
                    base = {"category_code": cat, "seasonally_adj": sa, "time_slot_id": "0"}
                    rows.append(
                        {
                            **base,
                            "cell_value": v,
                            "data_type_code": "TOTAL",
                            "error_data": "no",
                            "time": t,
                        }
                    )
                    rows.append(
                        {
                            **base,
                            "cell_value": "0.3",
                            "data_type_code": "E_TOTAL",
                            "error_data": "yes",
                            "time": t,
                        }
                    )
        else:
            for g, vals in self.wide.items():
                for y, (tot, pay) in vals.items():
                    rows.append(
                        {
                            "GRP": g,
                            "GRP_LABEL": self.wide_labels[g],
                            "TOT": tot,
                            "TOT_S": "3",
                            "PAY": pay,
                            "YEAR": y,
                            "time": y,
                        }
                    )
        rows = [r for r in rows if r["time"][:4] >= since[:4]]
        if not rows:
            return httpx.Response(204)
        header = [*get, "time", "us"]
        body = [header] + [[r.get(h, "1") for h in get] + [r["time"], "1"] for r in rows]
        return httpx.Response(200, json=body)


def make_adapter(tmp_path: Path, cfg: dict[str, Any]) -> CensusAdapter:
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", "census")
    fake = FakeCensus()
    adapter = CensusAdapter.from_config(
        cfg,
        paths,
        FAKE_KEY,
        transport=httpx.MockTransport(fake.handler),
        today=lambda: TODAY,
        sleep=lambda s: None,
    )
    adapter.fake = fake  # type: ignore[attr-defined]
    return adapter


def bump(adapter: CensusAdapter) -> None:
    """Nueva publicación del dataset largo: revisa el último valor de A/SA y añade 2026-10."""
    fake: FakeCensus = adapter.fake  # type: ignore[attr-defined]
    fake.modified["timeseries/demo/largo"] = "2026-11-01"
    adapter._datasets = None  # data.json se relee en la próxima corrida
    vals = fake.long[("A", "yes")]
    last = max(vals)
    vals[last] = "999.0"
    vals["2026-10"] = "1000.0"
