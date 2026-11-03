"""Servidor falso de download.bls.gov/pub/time.series para el test de contrato (sin red).

Formato real grabado a mano (TSV con cabecera, CRLF, columnas con espacios de relleno, listados
IIS): tres encuestas pequeñas y un directorio que no es encuesta.

- `cu`: dimensión de área con fila "U.S. city average", series SA/NSA (variantes), una
  regional, una semestral y una fila malformada; dos archivos de datos (todo + reciente).
- `jt`: sin `series_title` (título desde las tablas de códigos), estado con "Total US" y un
  área de una sola fila; trimestral con Q05 (promedio anual) que se descarta.
- `zz`: encuesta entera inactiva (≥3 series), para la fila agregada de linaje.
- `compressed/`: directorio sin `.series` (no es unidad).
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from src.ingest.bls import BlsAdapter
from src.ingest.common import SourcePaths

USER_AGENT = "Prueba Test prueba@example.org"
BASE = "https://download.bls.gov/pub/time.series/"


def _months(y0: int, y1: int, m1: int) -> list[tuple[int, str]]:
    return [(y, f"M{m:02d}") for y in range(y0, y1 + 1) for m in range(1, 13) if (y, m) <= (y1, m1)]


def _tsv(header: list[str], rows: list[list[str]], pad: int = 0) -> str:
    def fmt(cells: list[str]) -> str:
        return "\t".join([cells[0].ljust(pad), *cells[1:]]) if pad else "\t".join(cells)

    return "".join(fmt(r) + "\r\n" for r in [header, *rows])


class FakeBls:
    """Estado del servidor: archivos por ruta, con ETag y fecha de listado."""

    def __init__(self) -> None:
        self.version = 1
        self.requests: list[httpx.Request] = []
        # series_id -> {(year, period): value}
        self.values: dict[str, dict[tuple[int, str], str]] = {}
        months = _months(2000, 2026, 9)
        for sid in ["CUSR0000SA0", "CUUR0000SA0", "CUUR0100SA0", "JTS00Q"]:
            self.values[sid] = {}
        for k, (y, p) in enumerate(months):
            self.values["CUSR0000SA0"][(y, p)] = f"{100 + k * 0.1:.3f}"
            self.values["CUUR0000SA0"][(y, p)] = f"{100 + k * 0.11:.3f}"
            self.values["CUUR0100SA0"][(y, p)] = "-" if k == 5 else f"{90 + k * 0.1:.3f}"
        for y in range(2000, 2027):
            self.values["CUUR0000SA0"][(y, "M13")] = "150.0"  # promedio anual: se descarta
        self.values["CUUR0000SA0S1"] = {
            (y, f"S0{h}"): "1.0" for y in range(2000, 2026) for h in (1, 2)
        }
        for y in range(2001, 2027):
            for q in range(1, 6):
                if (y, q) <= (2026, 3):
                    self.values["JTS00Q"][(y, f"Q0{q}")] = f"{y + q / 10:.1f}"
        self.values["JTS06Q"] = dict(self.values["JTS00Q"])
        us, nsa = "All items in U.S. city average", "not seasonally adjusted"
        self.cu_series = {
            # sid: (area, seasonal, periodicity, title)
            "CUSR0000SA0": ("0000", "S", "R", f"{us}, seasonally adjusted"),
            "CUUR0000SA0": ("0000", "U", "R", f"{us}, {nsa}"),
            "CUUR0100SA0": ("0100", "U", "R", "All items in Northeast, not seasonally adjusted"),
            "CUUR0000SA0S1": ("0000", "U", "S", f"{us}, semiannual, {nsa}"),
        }  # fmt: skip
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, str] = {}
        self._build()

    # --- archivos ------------------------------------------------------------------------------

    def _ends(self, sid: str) -> tuple[str, str, str, str]:
        keys = sorted(self.values[sid], key=lambda k: (k[0], k[1]))
        (by, bp), (ey, ep) = keys[0], keys[-1]
        if ep.startswith("M13"):
            ey, ep = max((k for k in keys if k[1] != "M13"), key=lambda k: (k[0], k[1]))
        if ep == "Q05":
            ey, ep = max((k for k in keys if k[1] != "Q05"), key=lambda k: (k[0], k[1]))
        return str(by), bp, str(ey), ep

    def _data(self, sids: list[str], min_year: int = 0) -> str:
        rows = []
        for sid in sids:
            for (y, p), v in sorted(self.values[sid].items()):
                if y >= min_year:
                    rows.append([sid, str(y), p, v.rjust(12), ""])
        return _tsv(["series_id", "year", "period", "       value", "footnote_codes"], rows, 17)

    def _build(self) -> None:
        f: dict[str, str] = {}
        f["overview.txt"] = (
            "\t\t\tLIST OF DATABASES\r\n\r\n\tCU      Consumer Price Index-All Urban Consumers\r\n"
            "\tJT\tJob Openings and Labor Turnover Survey (NAICS)\r\n"
        )
        cu_rows = []
        for sid, (area, sa, per, title) in self.cu_series.items():
            cu_rows.append([sid, area, "SA0", sa, per, title, "", *self._ends(sid)])
        cu_rows.insert(2, ["CUBROKEN", "0000", "SA0"])  # fila malformada
        f["cu/cu.series"] = _tsv(
            ["series_id", "area_code", "item_code", "seasonal", "periodicity_code",
             "series_title", "footnote_codes", "begin_year", "begin_period", "end_year",
             "end_period"],
            cu_rows, 17,
        )  # fmt: skip
        f["cu/cu.area"] = _tsv(
            ["area_code", "area_name", "display_level", "selectable", "sort_sequence"],
            [["0000", "U.S. city average", "0", "T", "1"], ["0100", "Northeast", "0", "T", "5"]],
        )
        f["cu/cu.item"] = _tsv(["item_code", "item_name"], [["SA0", "All items"]])
        f["cu/cu.periodicity"] = _tsv(
            ["periodicity_code", "periodicity_name"], [["R", "Monthly"], ["S", "Semi-Annual"]]
        )
        f["cu/cu.seasonal"] = _tsv(
            ["seasonal_code", "seasonal_text"],
            [["S", "Seasonally Adjusted"], ["U", "Not Seasonally Adjusted"]],
        )
        f["cu/cu.txt"] = "Consumer Price Index (CU)\r\n"
        cu_all = list(self.cu_series)
        f["cu/cu.data.1.AllItems"] = self._data(cu_all)
        f["cu/cu.data.0.Current"] = self._data(cu_all, min_year=2025)

        jt_rows = []
        for sid, state in (("JTS00Q", "00"), ("JTS06Q", "06")):
            jt_rows.append([sid, "S", "000000", state, "00000", "JO", "L", "", *self._ends(sid)])
        f["jt/jt.series"] = _tsv(
            ["series_id", "seasonal", "industry_code", "state_code", "area_code",
             "dataelement_code", "ratelevel_code", "footnote_codes", "begin_year",
             "begin_period", "end_year", "end_period"],
            jt_rows, 30,
        )  # fmt: skip
        f["jt/jt.industry"] = _tsv(
            ["industry_code", "industry_text"], [["000000", "Total nonfarm"]]
        )
        f["jt/jt.state"] = _tsv(
            ["state_code", "state_text", "display_level"],
            [["00", "Total US", "0"], ["06", "California", "2"]],
        )
        f["jt/jt.area"] = _tsv(["area_code", "area_text"], [["00000", "All areas"]])
        f["jt/jt.dataelement"] = _tsv(
            ["dataelement_code", "dataelement_text"], [["JO", "Job openings"]]
        )
        f["jt/jt.ratelevel"] = _tsv(["ratelevel_code", "ratelevel_text"], [["L", "Level"]])
        f["jt/jt.data.1.AllItems"] = self._data(["JTS00Q", "JTS06Q"])

        zz = [
            [f"ZZU{i:05d}", "U", f"Vieja {i}", "", "1990", "M01", "2005", "M12"] for i in range(4)
        ]
        f["zz/zz.series"] = _tsv(
            ["series_id", "seasonal", "series_title", "footnote_codes", "begin_year",
             "begin_period", "end_year", "end_period"],
            zz,
        )  # fmt: skip
        f["zz/zz.data.0.All"] = _tsv(["series_id", "year", "period", "value", "footnote_codes"], [])
        f["compressed/cu.data.gz"] = "x"
        for path, text in f.items():
            data = text.encode()
            if self.files.get(path) != data:
                self.mtimes[path] = f"10/{self.version:02d}/2026  7:30 AM"
            self.files[path] = data

    # --- HTTP ----------------------------------------------------------------------------------

    def etag(self, path: str) -> str:
        return '"' + hashlib.sha1(self.files[path]).hexdigest()[:12] + ':0"'

    def _listing(self, directory: str) -> str:
        entries = []
        if directory == "":
            dirs = sorted({p.split("/")[0] for p in self.files if "/" in p})
            for d in dirs:
                link = f'<A HREF="/pub/time.series/{d}/">{d}</A>'
                entries.append(f"10/14/2026  7:30 AM        &lt;dir&gt; {link}")
            names = [p for p in self.files if "/" not in p]
        else:
            names = [p for p in self.files if p.startswith(directory) and p.count("/") == 1]
        for p in sorted(names):
            name = p.split("/")[-1]
            entries.append(
                f"{self.mtimes[p]:>19} {len(self.files[p]):>12} "
                f'<A HREF="/pub/time.series/{p}">{name}</A>'
            )
        body = "<br>".join(entries)
        return f'<html><body><pre><A HREF="/pub/">[To Parent Directory]</A><br><br>{body}<br></pre>'

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not request.headers.get("user-agent", "").strip():
            return httpx.Response(403)
        path = request.url.path.removeprefix("/pub/time.series/")
        if path == "" or path.endswith("/"):
            return httpx.Response(200, text=self._listing(path))
        if path not in self.files:
            return httpx.Response(404, text="not found")
        headers = {
            "ETag": self.etag(path),
            "Last-Modified": "Wed, 28 Oct 2026 14:00:00 GMT",
            "Content-Length": str(len(self.files[path])),
        }
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, headers=headers, content=self.files[path])

    # --- publicación nueva ---------------------------------------------------------------------

    def bump(self) -> None:
        """Revisa el último valor de la serie nacional SA y le agrega la observación siguiente."""
        self.version += 1
        vals = self.values["CUSR0000SA0"]
        last = max(k for k in vals if k[1] != "M13")
        vals[last] = "999.000"
        y, m = last[0], int(last[1][1:])
        nxt = (y + (m == 12), f"M{m % 12 + 1:02d}")
        vals[nxt] = "1000.000"
        self._build()


def make_adapter(tmp_path: Path, cfg: dict[str, Any]) -> BlsAdapter:
    server = FakeBls()
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", "bls")
    adapter = BlsAdapter(
        cfg,
        paths,
        user_agent=USER_AGENT,
        transport=httpx.MockTransport(server.handler),
        sleep=lambda s: None,
    )
    adapter.fake = server  # type: ignore[attr-defined]
    return adapter


def bump(adapter: BlsAdapter) -> None:
    adapter.fake.bump()  # type: ignore[attr-defined]
    adapter.reset_cache()


def today() -> date:
    return date(2026, 11, 3)
