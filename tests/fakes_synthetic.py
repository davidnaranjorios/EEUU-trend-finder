"""Adaptador sintético en memoria para probar la capa común (src/ingest/common.py).

Mismo contrato que los fakes de cada fuente (`tests/fakes_<fuente>.py`):
`make_adapter(tmp_path, cfg)` devuelve un adaptador sin red, y `bump(adapter)` simula una
publicación nueva (un valor revisado y una observación más) en una unidad con series objetivo.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src.ingest.common import Unit


def _months(start: date, n: int) -> list[date]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class SyntheticAdapter:
    source = "synthetic"

    def __init__(self, n_dead: int = 5) -> None:
        self.version = 1
        self.fetches: list[str] = []
        dates = _months(date(2000, 1, 1), 321)  # hasta 2026-09
        self.values: dict[str, dict[date, float]] = {
            "U1.NAC": {d: float(i) for i, d in enumerate(dates)},
            "U1.NAC.NSA": {d: float(i) + 0.5 for i, d in enumerate(dates)},
            "U1.REG": {d: 2.0 * i for i, d in enumerate(dates)},
            "U2.NAC": {d: 3.0 * i for i, d in enumerate(dates)},
        }
        self.n_dead = n_dead

    def list_units(self) -> list[Unit]:
        return [Unit("U1", f"v{self.version}"), Unit("U2", "v1"), Unit("DEAD", "v1")]

    def catalog_rows(self, unit: Unit):
        if unit.unit_id == "DEAD":
            for i in range(self.n_dead):
                yield _row(f"DEAD.{i}", "Vieja", date(1990, 1, 1), date(2010, 12, 1), "nacional")
            return
        for sid, vals in self.values.items():
            if not sid.startswith(unit.unit_id + "."):
                continue
            end = max(vals)
            level = "regional" if sid.endswith("REG") else "nacional"
            sa = "NSA" if sid.endswith("NSA") else "SA"
            title = "Título " + sid.split(".")[0] + ("" if level == "nacional" else " región")
            yield _row(sid, title, min(vals), end, level, sa=sa)

    def values_version(self, unit: Unit) -> str | None:
        return f"v{self.version}" if unit.unit_id == "U1" else "v1"

    def fetch_values(self, unit: Unit, series_ids: set[str]):
        self.fetches.append(unit.unit_id)
        for sid in sorted(series_ids):
            for d, v in sorted(self.values.get(sid, {}).items()):
                yield {
                    "series_id": sid,
                    "date": d,
                    "value": v,
                    "frequency": "M",
                    "units": "Índice",
                    "seasonal_adjustment": "SA",
                }

    def close(self) -> None:
        pass


def _row(sid: str, title: str, start: date, end: date, level: str, sa: str = "SA") -> dict:
    return {
        "series_id": sid,
        "title": title,
        "frequency": "M",
        "frequency_raw": "M",
        "units": "Índice",
        "seasonal_adjustment": sa,
        "observation_start": start,
        "observation_end": end,
        "last_updated": datetime(2026, 10, 1, tzinfo=UTC),
        "discontinued": False,
        "copyrighted": False,
        "geo_level": level,
        "geo_detail": None,
        "category_path": "sintética",
    }


def make_adapter(tmp_path: Path, cfg: dict[str, Any]) -> SyntheticAdapter:
    return SyntheticAdapter()


def bump(adapter: SyntheticAdapter) -> None:
    adapter.version += 1
    vals = adapter.values["U1.NAC"]
    last = max(vals)
    vals[last] += 100.0
    nxt = date(last.year + (last.month == 12), last.month % 12 + 1, 1)
    vals[nxt] = 999.0
