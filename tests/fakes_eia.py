"""Servidor falso de eia.gov/opendata/bulk para el test de contrato (sin red).

Un archivo masivo `ELEC.zip` con el formato real (un JSON por línea): una categoría, series
nacionales M y Q (datos de más nuevo a más viejo), una serie regional, una serie de planta con
`lat`/`lon` (se omite) y una línea malformada. Un `bump` publica una versión nueva (ETag).
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path

import httpx

from src.ingest.common import SourcePaths
from src.ingest.eia import EiaAdapter

BASE = "https://www.eia.gov/opendata/bulk/"


def _months(y0: int, end: tuple[int, int]) -> list[str]:
    return [f"{y}{m:02d}" for y in range(y0, end[0] + 1) for m in range(1, 13) if (y, m) <= end]


def _series(
    sid: str, name: str, f: str, iso: str, data: list, units: str = "cents per kilowatthour"
):
    return {
        "series_id": sid,
        "name": name,
        "units": units,
        "f": f,
        "copyright": "None",
        "source": "EIA, U.S. Energy Information Administration",
        "iso3166": iso,
        "geography": iso,
        "start": data[-1][0],
        "end": data[0][0],
        "last_updated": "2026-11-20T14:48:04-05:00",
        "data": data,
    }


class FakeEia:
    def __init__(self) -> None:
        self.version = 1
        self.requests: list[httpx.Request] = []
        months = _months(2001, (2026, 9))
        self.monthly = {
            "ELEC.PRICE.US-RES.M": [(p, 10 + 0.01 * i) for i, p in enumerate(months)],
            "ELEC.SALES.US-COM.M": [(p, 1000 + 2.0 * i) for i, p in enumerate(months)],
            "ELEC.PRICE.TX-RES.M": [(p, 9 + 0.01 * i) for i, p in enumerate(months)],
        }
        self.quarterly = {
            "ELEC.PRICE.US-RES.Q": [
                (f"{y}Q{q}", 10 + 0.1 * (4 * (y - 2001) + q))
                for y in range(2001, 2027)
                for q in range(1, 5)
                if (y, q) <= (2026, 3)
            ]
        }

    def zip_bytes(self) -> bytes:
        lines = [json.dumps({"category_id": "1", "name": "Precios", "childseries": []})]
        for sid, pts in self.monthly.items():
            iso = "USA-TX" if "TX" in sid else "USA"
            title = f"Serie {sid} : monthly"
            lines.append(
                json.dumps(_series(sid, title, "M", iso, [list(p) for p in reversed(pts)]))
            )
        for sid, pts in self.quarterly.items():
            title = f"Serie {sid} : quarterly"
            lines.append(
                json.dumps(_series(sid, title, "Q", "USA", [list(p) for p in reversed(pts)]))
            )
        plant = _series("ELEC.PLANT.GEN.1-ALL-ALL.M", "Planta", "M", "USA-TX", [["202609", 1.0]])
        plant |= {"lat": 30.0, "lon": -97.0, "latlon": "30.0,-97.0"}
        lines.append(json.dumps(plant))
        lines.append('{"series_id": "ELEC.ROTA" truncado')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("ELEC.txt", "\n".join(lines) + "\n")
        return buf.getvalue()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("ELEC.zip"):
            body = self.zip_bytes()
            headers = {
                "etag": f'"v{self.version}"',
                "last-modified": "Tue, 24 Nov 2026 15:52:00 GMT",
            }
            if request.method == "HEAD":
                return httpx.Response(200, headers={**headers, "content-length": str(len(body))})
            return httpx.Response(200, content=body, headers=headers)
        return httpx.Response(404)


def make_adapter(tmp_path: Path, cfg: dict) -> EiaAdapter:
    fake = FakeEia()
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", "eia")
    adapter = EiaAdapter(
        cfg, paths, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None
    )
    adapter.fake = fake  # type: ignore[attr-defined]
    return adapter


def bump(adapter: EiaAdapter) -> None:
    """Publicación nueva: un valor revisado y una observación más en la serie de precio mensual."""
    fake: FakeEia = adapter.fake  # type: ignore[attr-defined]
    fake.version += 1
    pts = fake.monthly["ELEC.PRICE.US-RES.M"]
    pts[-1] = (pts[-1][0], pts[-1][1] + 1.0)
    last = date(2026, 10, 1)
    pts.append((f"{last.year}{last.month:02d}", 20.0))
    adapter.reset_cache()
