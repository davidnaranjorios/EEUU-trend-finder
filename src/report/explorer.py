"""Fichas mínimas del explorador de series del dashboard (4 fuentes, sin copyright).

Universo: la última decisión del embudo de cada serie en los grupos `download.groups`. Una ficha
lleva fuente, id, título, frecuencia, unidades, el menor q de la última corrida numérica (y su
detector), cuántos términos se enlazaron y el candidato publicado al que pertenece, si alguno.
Formato columnar en `data/explorer/NN.json` (`report.explorer.n_buckets` buckets por fuente|id),
con tope de tamaño (`report.explorer.max_file_kb`, `max_total_mb`; falla sin escribir).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import astuple, dataclass, fields
from typing import Any

import duckdb

from src.export import check_size
from src.ingest.store import bucket_of


@dataclass
class Card:
    """Ficha del explorador (orden de columnas del JSON y de site.js)."""

    source: str
    id: str
    title: str
    frequency: str
    units: str
    q: float | None
    detector: str | None
    links: int
    candidate: str | None


COLUMNS = [f.name for f in fields(Card)]


def explorer_files(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    *,
    numeric_run: str | None,
    rank_run: str | None,
    published: dict[tuple[str, str], str],
    forbidden: set[tuple[str, str]],
) -> dict[str, bytes]:
    ecfg = cfg["report"]["explorer"]
    groups = list(cfg["download"]["groups"])
    rows = con.execute(
        """
        WITH last AS (
          SELECT source, series_id, group_name, frequency, copyrighted FROM eligibility
          QUALIFY row_number() OVER (PARTITION BY source, series_id
                                     ORDER BY decided_at DESC, run_id DESC) = 1
        ), ok AS (
          SELECT * FROM last WHERE list_contains(?, group_name) AND NOT copyrighted
        ), u AS (
          SELECT source, series_id, any_value(units) AS units FROM series
          WHERE vintage_date IS NULL GROUP BY ALL
        ), q AS (
          SELECT source, object_id AS series_id, min(q_value) AS q,
                 arg_min(detector, q_value) AS det
          FROM detector_tests WHERE run_id = ? GROUP BY ALL
        ), l AS (
          SELECT source, series_id, count(*) AS n FROM series_term_links
          WHERE run_id = ? GROUP BY ALL
        )
        SELECT ok.source, ok.series_id, m.title, ok.frequency, u.units, q.q, q.det, l.n,
               coalesce(m.copyrighted, false)
        FROM ok LEFT JOIN series_meta m USING (source, series_id)
        LEFT JOIN u USING (source, series_id) LEFT JOIN q USING (source, series_id)
        LEFT JOIN l USING (source, series_id)
        ORDER BY 1, 2
        """,
        [groups, numeric_run or "", rank_run or ""],
    ).fetchall()
    n_buckets = ecfg["n_buckets"]
    buckets: dict[int, list[list[Any]]] = defaultdict(list)
    by_source: Counter[str] = Counter()
    withheld = 0
    for src, sid, title, freq, units, q, det, n, cr in rows:
        if cr or (src, sid) in forbidden:
            withheld += 1
            continue
        by_source[src] += 1
        card = Card(
            source=src,
            id=sid,
            title=title or "",
            frequency=freq or "",
            units=units or "",
            q=float(f"{q:.3g}") if q is not None else None,
            detector=det,
            links=int(n or 0),
            candidate=published.get((src, sid)),
        )
        buckets[bucket_of(f"{src}|{sid}", n_buckets)].append(list(astuple(card)))
    files = {
        f"data/explorer/{b:02d}.json": _dumps({"columns": COLUMNS, "rows": r})
        for b, r in sorted(buckets.items())
    }
    files["data/explorer/index.json"] = _dumps(
        {
            "n_buckets": n_buckets,
            "buckets": sorted(buckets),
            "n_cards": sum(by_source.values()),
            "by_source": dict(sorted(by_source.items())),
            "copyrighted_withheld": withheld,
            "columns": COLUMNS,
        }
    )
    check_size(files, ecfg)
    return files


def _dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode()
