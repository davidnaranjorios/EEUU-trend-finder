"""Servidor FRED en memoria para tests (formato JSON de la API real).

Reproduce lo que el adaptador usa: category/children, category/series, series/observations
(con rango realtime y recorte de realtime_start), series/updates y series/vintagedates, con
paginación limit/offset, errores inyectables y registro de solicitudes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

REALTIME_MIN = "1776-07-04"
REALTIME_MAX = "9999-12-31"


def monthly_dates(start: date, n: int) -> list[date]:
    out = []
    y, m = start.year, start.month
    for _ in range(n):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def make_info(
    sid: str,
    *,
    frequency_short: str = "M",
    start: str = "2000-01-01",
    end: str = "2026-08-01",
    last_updated: str = "2026-10-15 07:50:02-05",
    title: str | None = None,
    notes: str | None = "Notas de prueba.",
) -> dict[str, Any]:
    info = {
        "id": sid,
        "realtime_start": "2026-10-20",
        "realtime_end": "2026-10-20",
        "title": title or f"Serie {sid}",
        "observation_start": start,
        "observation_end": end,
        "frequency": "Monthly",
        "frequency_short": frequency_short,
        "units": "Index",
        "units_short": "Index",
        "seasonal_adjustment": "Seasonally Adjusted",
        "seasonal_adjustment_short": "SA",
        "last_updated": last_updated,
        "popularity": 1,
    }
    if notes is not None:
        info["notes"] = notes
    return info


class FakeFred:
    def __init__(self) -> None:
        self.categories: dict[int, tuple[str, list[int]]] = {0: ("Categories", [])}
        self.members: dict[int, list[str]] = {}
        self.infos: dict[str, dict[str, Any]] = {}
        self.obs: dict[str, list[dict[str, str]]] = {}
        self.updates: list[str] = []
        self.vintage_cap: set[str] = set()
        self.missing: set[str] = set()
        self.fail_queue: list[int] = []
        self.always_fail: int | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []

    # --- construcción de datos ---

    def add_category(self, cid: int, name: str, parent: int) -> None:
        self.categories[cid] = (name, [])
        self.categories[parent][1].append(cid)

    def add_series(self, cid: int, info: dict[str, Any], n_obs: int = 0) -> None:
        sid = info["id"]
        self.infos[sid] = info
        self.members.setdefault(cid, []).append(sid)
        if sid not in self.obs:
            start = date.fromisoformat(info["observation_start"])
            self.obs[sid] = [
                {
                    "realtime_start": _plus_days(d, 45),
                    "realtime_end": REALTIME_MAX,
                    "date": d.isoformat(),
                    "value": f"{100 + i * 0.5:.1f}",
                }
                for i, d in enumerate(monthly_dates(start, n_obs))
            ]

    def revise(self, sid: str, obs_date: str, new_value: str, on: str) -> None:
        """Revisa una observación: cierra la vintage vigente y abre una nueva."""
        for row in self.obs[sid]:
            if row["date"] == obs_date and row["realtime_end"] == REALTIME_MAX:
                row["realtime_end"] = _minus_one_day(on)
        self.obs[sid].append(
            {
                "realtime_start": on,
                "realtime_end": REALTIME_MAX,
                "date": obs_date,
                "value": new_value,
            }
        )

    # --- transporte ---

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, endpoint: str, **match: str) -> int:
        return sum(
            1
            for path, params in self.requests
            if path == endpoint and all(params.get(k) == v for k, v in match.items())
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.split("/fred/", 1)[1]
        params = dict(request.url.params)
        self.requests.append((endpoint, params))
        if self.always_fail is not None:
            return httpx.Response(self.always_fail, json={"error_message": "falla"})
        if self.fail_queue:
            return httpx.Response(self.fail_queue.pop(0), json={"error_message": "falla"})
        handler = getattr(self, "_" + endpoint.replace("/", "_"))
        return handler(params)

    def _category_children(self, p: dict[str, str]) -> httpx.Response:
        cid = int(p["category_id"])
        children = [
            {"id": c, "name": self.categories[c][0], "parent_id": cid}
            for c in self.categories[cid][1]
        ]
        return httpx.Response(200, json={"categories": children})

    def _category_series(self, p: dict[str, str]) -> httpx.Response:
        items = [self.infos[s] for s in sorted(self.members.get(int(p["category_id"]), []))]
        return _page(p, items, "seriess")

    def _series_updates(self, p: dict[str, str]) -> httpx.Response:
        return _page(p, [self.infos[s] for s in self.updates], "seriess")

    def _series_vintagedates(self, p: dict[str, str]) -> httpx.Response:
        dates = sorted({r["realtime_start"] for r in self.obs[p["series_id"]]}, reverse=True)
        return httpx.Response(200, json={"vintage_dates": dates[: int(p.get("limit", 10000))]})

    def _series_observations(self, p: dict[str, str]) -> httpx.Response:
        sid = p["series_id"]
        if sid in self.missing:
            return httpx.Response(400, json={"error_message": "The series does not exist."})
        rs, re_ = p.get("realtime_start", REALTIME_MAX), p.get("realtime_end", REALTIME_MAX)
        if sid in self.vintage_cap and rs == REALTIME_MIN:
            return httpx.Response(
                400,
                json={"error_message": "This exceeds the maximum number of vintage dates (2000)."},
            )
        rows = []
        for r in self.obs[sid]:
            if r["realtime_end"] >= rs and r["realtime_start"] <= re_:
                rows.append({**r, "realtime_start": max(r["realtime_start"], rs)})
        rows.sort(key=lambda r: (r["date"], r["realtime_start"]))
        return _page(p, rows, "observations")


def _page(p: dict[str, str], items: list[Any], key: str) -> httpx.Response:
    limit, offset = int(p.get("limit", 1000)), int(p.get("offset", 0))
    body = {
        "count": len(items),
        "offset": offset,
        "limit": limit,
        key: items[offset : offset + limit],
    }
    return httpx.Response(200, json=body)


def _plus_days(d: date, n: int) -> str:
    return date.fromordinal(d.toordinal() + n).isoformat()


def _minus_one_day(iso: str) -> str:
    return date.fromordinal(date.fromisoformat(iso).toordinal() - 1).isoformat()
