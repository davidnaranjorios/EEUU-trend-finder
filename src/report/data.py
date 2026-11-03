"""Datos del reporte y del dashboard, ya filtrados para publicar (primera barrera de copyright).

Toda serie con `copyrighted = true` (en `series_meta`, en el embudo o en la evidencia) queda fuera:
no aparece su id, ni su título, ni sus valores. Un candidato se publica solo si
`rank_candidates.published` (su evidencia pública tiene ≥ `rank.min_sources` fuentes).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

import duckdb
import pyarrow as pa

from src.detect.run import RUNS_PARTITION, _resolve, table_root
from src.detect.text import grid_start
from src.ingest.store import read_table
from src.rank.run import NUMERIC_RUN_PARTITIONS, PARTITION, latest_rank_run, rank_root, term_text
from src.report.charts import line_chart
from src.schemas import DETECTOR_RUNS, RANK_CANDIDATES, RANK_EVIDENCE

UNIT_WILDCARD = "/*"


def copyrighted_series(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str | None]]:
    """(fuente, id, título) de toda serie marcada con copyright en metadatos o embudo."""
    return con.execute(
        """
        SELECT source, series_id, any_value(title) FROM (
          SELECT source, series_id, title FROM series_meta WHERE copyrighted
          UNION ALL
          SELECT e.source, e.series_id, m.title FROM eligibility e
          LEFT JOIN series_meta m USING (source, series_id) WHERE e.copyrighted
          UNION ALL
          SELECT source, object_id, NULL FROM detector_tests WHERE copyrighted
        ) GROUP BY ALL
        """
    ).fetchall()


# --- candidatos ----------------------------------------------------------------------------------


def _rank_rows(cfg: dict[str, Any], spec: Any, table: str, run_id: str) -> list[dict[str, Any]]:
    return read_table(
        spec, rank_root(cfg, table), source=PARTITION, where="run_id = ?", params=[run_id]
    ).to_pylist()


def candidates_data(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    rank_run: dict[str, Any],
    forbidden: set[tuple[str, str]],
    *,
    published_only: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Candidatos publicables con su evidencia, por tipo (`series`, `text`), en orden de rango."""
    run_id = rank_run["run_id"]
    cands = [
        c
        for c in _rank_rows(cfg, RANK_CANDIDATES, "rank_candidates", run_id)
        if c["status"] == "candidato" and (c["published"] or not published_only)
    ]
    ev = defaultdict(list)
    for e in _rank_rows(cfg, RANK_EVIDENCE, "rank_evidence", run_id):
        ev[e["candidate_id"]].append(e)
    keys = {
        (e["source"], e["object_id"])
        for c in cands
        for e in ev[c["candidate_id"]]
        if e["item_kind"] == "series"
        and e["public"]
        and (e["source"], e["object_id"]) not in forbidden
    }
    meta = _series_meta(con, keys)
    values = _series_values(con, keys, cfg)
    term_keys = {
        (e["source"], e["object_id"])
        for c in cands
        for e in ev[c["candidate_id"]]
        if e["item_kind"] == "term"
    }
    shares = _term_shares(con, cfg, term_keys)
    out: dict[str, list[dict[str, Any]]] = {"series": [], "text": []}
    for c in sorted(cands, key=lambda c: (c["kind"], c["rank"] or 0)):
        items = ev[c["candidate_id"]]
        series, hidden = [], 0
        for e in (i for i in items if i["item_kind"] == "series"):
            k = (e["source"], e["object_id"])
            if not e["public"] or k in forbidden:
                hidden += 1
                continue
            m = meta.get(k, {})
            d, v = values.get(k, ([], []))
            series.append(
                {
                    "source": e["source"],
                    "id": e["object_id"],
                    "title": m.get("title") or e["object_id"],
                    "units": m.get("units") or "",
                    "frequency": m.get("frequency") or "",
                    "seasonal_adjustment": m.get("seasonal_adjustment") or "",
                    "detectors": e["detectors"].split(","),
                    "q_value": e["q_value"],
                    "effect_size": e["effect_size"],
                    "window": [e["window_start"], e["window_end"]],
                    "agencies": json.loads(e["agencies_json"]),
                    "chart": line_chart(
                        d,
                        v,
                        label=m.get("title") or e["object_id"],
                        window=(e["window_start"], e["window_end"]) if e["window_start"] else None,
                        log_scale=bool(v) and min(v) > 0,
                    ),
                }
            )
        terms = []
        for e in (i for i in items if i["item_kind"] == "term"):
            section = e["object_id"].split("|", 1)[0]
            linked = e["linked_series"]
            if linked and tuple(linked.split("|", 1)) in forbidden:
                linked = None
            d, v = shares.get((e["source"], e["object_id"]), ([], []))
            terms.append(
                {
                    "source": e["source"],
                    "section": section,
                    "term": term_text(e["object_id"]),
                    "q_value": e["q_value"],
                    "effect_size": e["effect_size"],
                    "n_entities": e["n_entities"],
                    "link_score": e["link_score"],
                    "linked_series": linked,
                    "window": [e["window_start"], e["window_end"]],
                    "chart": line_chart(
                        d,
                        v,
                        label=f"Proporción de documentos: {term_text(e['object_id'])}",
                        window=(e["window_start"], e["window_end"]) if e["window_start"] else None,
                        value_format="share",
                    ),
                }
            )
        out[c["kind"]].append(
            {
                "candidate_id": c["candidate_id"],
                "kind": c["kind"],
                "rank": c["rank"],
                "score": c["score"],
                "n_sources": c["n_sources_public"],
                "sources": json.loads(c["sources_json"]),
                "components": json.loads(c["components_json"] or "{}"),
                "n_members": c["n_members"],
                "series": series,
                "hidden_series": hidden,
                "terms": sorted(terms, key=lambda t: t["q_value"]),
            }
        )
    return out


def _register(con: duckdb.DuckDBPyConnection, name: str, keys: set[tuple[str, str]]) -> None:
    ks = sorted(keys)
    con.register(name, pa.table({"source": [k[0] for k in ks], "series_id": [k[1] for k in ks]}))


def _series_meta(
    con: duckdb.DuckDBPyConnection, keys: set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    if not keys:
        return {}
    _register(con, "_mk", keys)
    rows = con.execute(
        """
        SELECT k.source, k.series_id, any_value(m.title), any_value(s.units),
               any_value(s.frequency), any_value(s.seasonal_adjustment)
        FROM _mk k LEFT JOIN series_meta m USING (source, series_id)
        LEFT JOIN (SELECT source, series_id, units, frequency, seasonal_adjustment FROM series
                   JOIN _mk USING (source, series_id) WHERE vintage_date IS NULL) s
          USING (source, series_id)
        GROUP BY ALL
        """
    ).fetchall()
    return {
        (r[0], r[1]): {
            "title": r[2],
            "units": r[3],
            "frequency": r[4],
            "seasonal_adjustment": r[5],
        }
        for r in rows
    }


def _series_values(
    con: duckdb.DuckDBPyConnection, keys: set[tuple[str, str]], cfg: dict[str, Any]
) -> dict[tuple[str, str], tuple[list[date], list[float]]]:
    if not keys:
        return {}
    _register(con, "_vk", keys)
    years = cfg["report"]["chart_years"]
    rows = con.execute(
        f"""
        SELECT source, series_id, list(date ORDER BY date), list(value ORDER BY date)
        FROM series JOIN _vk USING (source, series_id)
        WHERE vintage_date IS NULL AND value IS NOT NULL AND isfinite(value)
          AND date >= (SELECT max(date) FROM series s2 WHERE s2.source = series.source
                       AND s2.series_id = series.series_id) - INTERVAL {int(years)} YEAR
        GROUP BY ALL
        """
    ).fetchall()
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


def _term_shares(
    con: duckdb.DuckDBPyConnection, cfg: dict[str, Any], keys: set[tuple[str, str]]
) -> dict[tuple[str, str], tuple[list[date], list[float]]]:
    """Proporción de documentos por periodo de la grilla del detector (0 si faltó el término)."""
    if not keys:
        return {}
    ks = sorted(keys)
    con.register(
        "_tk",
        pa.table(
            {
                "source": [k[0] for k in ks],
                "section": [k[1].split("|", 1)[0] for k in ks],
                "term": [term_text(k[1]) for k in ks],
            }
        ),
    )
    totals = con.execute(
        """
        SELECT f.source, f.section, f.period, max(f.total_docs) FROM term_freq f
        WHERE (f.source, f.section) IN (SELECT DISTINCT source, section FROM _tk) GROUP BY ALL
        """
    ).fetchall()
    counts = con.execute(
        "SELECT source, section, term, period, doc_count FROM term_freq JOIN _tk "
        "USING (source, section, term)"
    ).fetchall()
    grids = cfg["text_detect"]["grid"]
    tot: dict[tuple[str, str, str], int] = defaultdict(int)
    for src, section, period, n in totals:
        tot[(src, section, _to_grid(period, grids.get(src, "A")))] += int(n)
    cnt: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for src, section, term, period, n in counts:
        cnt[(src, section, term, _to_grid(period, grids.get(src, "A")))] += int(n)
    out = {}
    for src, oid in ks:
        section, term = oid.split("|", 1)[0], term_text(oid)
        periods = sorted(p for (s, sec, p) in tot if s == src and sec == section)
        out[(src, oid)] = (
            [grid_start(p) for p in periods],
            [cnt.get((src, section, term, p), 0) / tot[(src, section, p)] for p in periods],
        )
    return out


def _to_grid(period: str, grid: str) -> str:
    if grid == "A":
        return period[:4]
    if grid == "Q":
        if "Q" in period or len(period) == 4:
            return period
        return f"{period[:4]}Q{(int(period[5:7]) - 1) // 3 + 1}"
    return period


# --- dashboard -----------------------------------------------------------------------------------


def system_status(cfg: dict[str, Any], rank_run: dict[str, Any] | None) -> dict[str, Any]:
    """Últimas corridas: registro de ejecuciones, detectores y ranking."""
    last: dict[str, dict[str, Any]] = {}
    # Registro compartido (ingesta y detectores) y el propio del reporte (logs/reporte/).
    files = [_resolve(cfg["logging"]["runs_file"]), _resolve(cfg["report"]["runs_file"])]
    lines = [ln for f in files if f.exists() for ln in f.read_text("utf-8").splitlines()]
    if lines:
        for line in lines:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            cmd = (r.get("details") or {}).get("command") or ",".join(r.get("sources") or [])
            key = f"{cmd} [{','.join(r.get('sources') or [])}]"
            last[key] = {
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "status": r.get("status"),
                "total_seconds": r.get("total_seconds"),
                "n_series": r.get("n_series"),
            }
    detectors = []
    for part in sorted({RUNS_PARTITION, *NUMERIC_RUN_PARTITIONS, *cfg["text_detect"]["grid"]}):
        rows = read_table(DETECTOR_RUNS, table_root(cfg, "detector_runs"), source=part).to_pylist()
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["detector"] not in latest or r["started_at"] > latest[r["detector"]]["started_at"]:
                latest[r["detector"]] = r
        for det, r in sorted(latest.items()):
            detectors.append(
                {
                    "family": part,
                    "detector": det,
                    "run_id": r["run_id"],
                    "finished_at": r["finished_at"],
                    "n_tests": r["n_tests"],
                    "n_bh": r["n_bh"],
                    "n_by": r["n_by"],
                    "q": r["q"],
                }
            )
    return {
        "generated_at": datetime.now(UTC).replace(microsecond=0),
        "runs": dict(sorted(last.items())),
        "detectors": detectors,
        "rank": {k: v for k, v in (rank_run or {}).items() if k != "params_json"},
    }


def funnel(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Embudo por fuente y grupo: última decisión por serie (las agregadas por su n_series)."""
    rows = con.execute(
        f"""
        WITH last AS (
          SELECT source, series_id, group_name, rule, detail, copyrighted FROM eligibility
          QUALIFY row_number() OVER (PARTITION BY source, series_id
                                     ORDER BY decided_at DESC, run_id DESC) = 1
        )
        SELECT source, group_name, rule,
               sum(CASE WHEN series_id LIKE '%{UNIT_WILDCARD}'
                   THEN coalesce(
                     TRY_CAST(regexp_extract(detail, 'n_series=(\\d+)', 1) AS BIGINT), 1)
                   ELSE 1 END) AS n,
               sum(CASE WHEN copyrighted THEN 1 ELSE 0 END) AS n_copyrighted
        FROM last GROUP BY ALL ORDER BY source, group_name, rule
        """
    ).fetchall()
    return [
        {"source": s, "group": g, "rule": r, "n": int(n), "n_copyrighted": int(c)}
        for s, g, r, n, c in rows
    ]


def coverage(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    numeric = con.execute(
        """
        SELECT source, frequency, count(DISTINCT series_id), count(*), min(date), max(date)
        FROM series WHERE vintage_date IS NULL GROUP BY ALL ORDER BY 1, 2
        """
    ).fetchall()
    text = con.execute(
        """
        SELECT source, section, count(DISTINCT period), min(period), max(period),
               sum(total_docs) FROM (
          SELECT source, section, period, max(total_docs) AS total_docs FROM term_freq GROUP BY ALL
        ) GROUP BY ALL ORDER BY 1, 2
        """
    ).fetchall()
    vint = con.execute(
        "SELECT source, count(DISTINCT series_id) FROM series WHERE vintage_date IS NOT NULL "
        "GROUP BY ALL ORDER BY 1"
    ).fetchall()
    return {
        "numeric": [
            {
                "source": s,
                "frequency": f,
                "n_series": int(n),
                "n_obs": int(o),
                "start": a,
                "end": b,
            }
            for s, f, n, o, a, b in numeric
        ],
        "vintages": [{"source": s, "n_series": int(n)} for s, n in vint],
        "text": [
            {
                "source": s,
                "section": sec,
                "n_periods": int(n),
                "start": a,
                "end": b,
                "n_docs": int(d or 0),
            }
            for s, sec, n, a, b, d in text
        ],
    }


def load_rank_run(cfg: dict[str, Any], run_id: str | None) -> dict[str, Any] | None:
    if run_id is None:
        return latest_rank_run(cfg)
    from src.schemas import RANK_RUNS

    rows = read_table(
        RANK_RUNS,
        rank_root(cfg, "rank_runs"),
        source=PARTITION,
        where="run_id = ?",
        params=[run_id],
    ).to_pylist()
    return rows[0] if rows else None
