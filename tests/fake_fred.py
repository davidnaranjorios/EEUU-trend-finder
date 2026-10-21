"""Servidor FRED en memoria para tests (formato JSON de la API real).

Reproduce lo que el adaptador usa: category/children, category/series, series/observations
(con rango realtime y recorte de realtime_start), series/updates, series/vintagedates, tags,
tags/series (con `count` inestable, como la API real), releases, release/sources,
release/series y v2/release/observations (cursor, auth por header), con paginación
limit/offset, errores inyectables y registro de solicitudes.
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
        self.max_vintages = 2000
        self.missing: set[str] = set()
        self.fail_queue: list[int] = []
        self.always_fail: int | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.headers: list[dict[str, str]] = []
        self.today = "2026-10-20"
        # Metadatos de crawl-meta: tags por serie (grupo geot + "usa") y releases.
        self.geot = ["nation", "state", "county", "transnational"]
        self.tags_of: dict[str, set[str]] = {}
        self.releases: dict[int, tuple[str, list[str]]] = {}  # id -> (agencia, series)

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

    def set_meta(self, sid: str, tags: set[str], release: int, agency: str) -> None:
        self.tags_of[sid] = tags
        name, members = self.releases.setdefault(release, (agency, []))
        members.append(sid)

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
        self.headers.append(dict(request.headers))
        if self.always_fail is not None:
            return httpx.Response(self.always_fail, json={"error_message": "falla"})
        if self.fail_queue:
            return httpx.Response(self.fail_queue.pop(0), json={"error_message": "falla"})
        if endpoint.startswith("v2/") and "Bearer " not in request.headers.get("authorization", ""):
            return httpx.Response(401, json={"error_message": "falta la API key"})
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
        dates = sorted({r["realtime_start"] for r in self.obs[p["series_id"]]})
        if p.get("sort_order") == "desc":
            dates.reverse()
        return _page(p, dates, "vintage_dates")

    def _tags(self, p: dict[str, str]) -> httpx.Response:
        assert p["tag_group_id"] == "geot"
        return _page(p, [{"name": t, "group_id": "geot"} for t in self.geot], "tags")

    def _tags_series(self, p: dict[str, str]) -> httpx.Response:
        tag = p["tag_names"]
        items = [self.infos[s] for s in sorted(self.tags_of) if tag in self.tags_of[s]]
        resp = _page(p, items, "seriess")
        body = resp.json()
        body["count"] = len(items) + 7  # la API real devuelve un count que no cuadra
        return httpx.Response(200, json=body)

    def _releases(self, p: dict[str, str]) -> httpx.Response:
        return _page(
            p, [{"id": rid, "name": f"R{rid}"} for rid in sorted(self.releases)], "releases"
        )

    def _release_sources(self, p: dict[str, str]) -> httpx.Response:
        agency = self.releases[int(p["release_id"])][0]
        return httpx.Response(200, json={"sources": [{"id": 1, "name": agency}]})

    def _release_series(self, p: dict[str, str]) -> httpx.Response:
        members = sorted(self.releases[int(p["release_id"])][1])
        return _page(p, [self.infos[s] for s in members], "seriess")

    def _v2_release_observations(self, p: dict[str, str]) -> httpx.Response:
        """Cursor 'SERIE,FECHA': la página sigue desde esa observación (incluida)."""
        limit = int(p.get("limit", 500000))
        members = sorted(self.releases[int(p["release_id"])][1])
        flat = [
            (sid, o["date"], o["value"])
            for sid in members
            for o in sorted(self.obs[sid], key=lambda o: o["date"])
            if o["realtime_end"] == REALTIME_MAX
        ]
        start = 0
        if "next_cursor" in p:
            sid, d = p["next_cursor"].split(",")
            start = next(i for i, (s, dd, _) in enumerate(flat) if (s, dd) == (sid, d))
        chunk = flat[start : start + limit]
        series: list[dict[str, Any]] = []
        for sid, d, v in chunk:
            if not series or series[-1]["series_id"] != sid:
                info = self.infos[sid]
                series.append(
                    {
                        "series_id": sid,
                        "title": info["title"],
                        "frequency": info["frequency"],
                        "units": info["units"],
                        "seasonal_adjustment": info["seasonal_adjustment"],
                        "last_updated": "2026-10-15T12:50:02Z",
                        "copyright_id": "public domain: citation requested",
                        "notes": "",
                        "observations": [],
                    }
                )
            series[-1]["observations"].append({"date": d, "value": v})
        body: dict[str, Any] = {
            "has_more": start + limit < len(flat),
            "release": {"release_id": int(p["release_id"])},
            "series": series,
        }
        if body["has_more"]:
            nxt = flat[start + limit]
            body["next_cursor"] = f"{nxt[0]},{nxt[1]}"
        return httpx.Response(200, json=body)

    def _series_observations(self, p: dict[str, str]) -> httpx.Response:
        sid = p["series_id"]
        if sid in self.missing:
            return httpx.Response(400, json={"error_message": "The series does not exist."})
        # Como la API real: sin rango realtime, el rango es "hoy".
        rs, re_ = p.get("realtime_start", self.today), p.get("realtime_end", self.today)
        n_vintages = len(
            {r["realtime_start"] for r in self.obs[sid] if rs <= r["realtime_start"] <= re_}
        )
        if n_vintages > self.max_vintages:
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
