"""Corrida del ranking: clusters, cruces término–serie, triangulación y score (ADR 0009).

Uso: `uv run python -m src.rank run [--numeric-run ID] [--text-runs edgar=ID,...]`

Entradas (las últimas corridas, salvo que se indiquen):
- señales numéricas (`signals`) de la última corrida de los detectores numéricos;
- señales de texto (`text_emerging`) de la última corrida de cada fuente de texto;
- `detector_tests` (universo de series probadas), `series_meta`, `series`, `term_freq` y las
  agencias de FRED (`fred_series_attrs`).

Salidas en `processed/rank/<tabla>/rank/NN/`: `clusters`, `series_term_links`,
`rank_candidates`, `rank_evidence` y `rank_runs`. Todo es upsert por `run_id`: repetir una
corrida con el mismo id no duplica filas.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa

from src.catalog import connect
from src.config import load_config
from src.detect.run import _resolve, detect_sources, load_series, prep_for, table_root
from src.detect.text import DETECTOR as TEXT_DETECTOR
from src.detect.text import grid_start
from src.ingest.fred import SERIES_ATTRS
from src.ingest.store import read_table, upsert
from src.log import get_logger, run
from src.rank.agencies import series_agencies, text_agencies, unmatched_adapter_agencies
from src.rank.cluster import (
    ClusterParams,
    GrowthSeries,
    cluster_id,
    cluster_labels,
    growth_correlation,
    similarity,
)
from src.rank.embed import CachedEmbedder, Embedder, make_embedder
from src.rank.links import LinkParams, null_threshold, sample_null, top_links
from src.rank.triangulate import Candidate, Evidence, assess, score
from src.schemas import (
    CLUSTERS,
    DETECTOR_RUNS,
    RANK_CANDIDATES,
    RANK_EVIDENCE,
    RANK_RUNS,
    SERIES_TERM_LINKS,
    SIGNALS,
)

PARTITION = "rank"
N_BUCKETS = 8
# Particiones de detector_runs de la familia numérica (la de la Fase 2 se escribía en "fred").
NUMERIC_RUN_PARTITIONS = ("numeric", "fred")
RANK_TABLES = {
    "clusters": CLUSTERS,
    "series_term_links": SERIES_TERM_LINKS,
    "rank_candidates": RANK_CANDIDATES,
    "rank_evidence": RANK_EVIDENCE,
    "rank_runs": RANK_RUNS,
}


def rank_root(cfg: dict[str, Any], table: str) -> Path:
    return _resolve(cfg["paths"]["processed_dir"]) / "rank" / table


def text_sources(cfg: dict[str, Any]) -> list[str]:
    return sorted(cfg["text_detect"]["grid"])


# --- corridas de entrada -------------------------------------------------------------------------


def latest_numeric_run(cfg: dict[str, Any]) -> str | None:
    rows: list[dict[str, Any]] = []
    for part in NUMERIC_RUN_PARTITIONS:
        rows += read_table(DETECTOR_RUNS, table_root(cfg, "detector_runs"), source=part).to_pylist()
    rows = [r for r in rows if r["detector"] in cfg["detect"]["detectors"]]
    if not rows:
        return None
    return max(rows, key=lambda r: (r["started_at"], r["run_id"]))["run_id"]


def latest_text_runs(cfg: dict[str, Any]) -> dict[str, str]:
    out = {}
    for src in text_sources(cfg):
        rows = [
            r
            for r in read_table(
                DETECTOR_RUNS, table_root(cfg, "detector_runs"), source=src
            ).to_pylist()
            if r["detector"] == TEXT_DETECTOR
        ]
        if rows:
            out[src] = max(rows, key=lambda r: (r["started_at"], r["run_id"]))["run_id"]
    return out


def _signals(cfg: dict[str, Any], source: str, run_id: str) -> list[dict[str, Any]]:
    return read_table(
        SIGNALS, table_root(cfg, "signals"), source=source, where="run_id = ?", params=[run_id]
    ).to_pylist()


# --- datos de series -----------------------------------------------------------------------------


@dataclass
class SeriesInfo:
    source: str
    series_id: str
    title: str
    units: str
    copyrighted: bool
    agencies: frozenset[str]

    @property
    def key(self) -> str:
        return f"{self.source}|{self.series_id}"


def tested_series(
    con: duckdb.DuckDBPyConnection, numeric_run: str | None
) -> list[tuple[str, str, bool]]:
    if numeric_run is None:
        return []
    return con.execute(
        "SELECT source, object_id, bool_or(copyrighted) FROM detector_tests WHERE run_id = ? "
        "GROUP BY ALL ORDER BY 1, 2",
        [numeric_run],
    ).fetchall()


def series_info(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    keys: list[tuple[str, str, bool]],
    with_units: set[tuple[str, str]],
) -> dict[tuple[str, str], SeriesInfo]:
    """Título, unidades (solo de `with_units`), copyright y agencias de cada serie."""
    if not keys:
        return {}
    con.register(
        "_keys",
        pa.table(
            {
                "source": [k[0] for k in keys],
                "series_id": [k[1] for k in keys],
                "cr": [k[2] for k in keys],
            }
        ),
    )
    meta = {
        (s, i): (t, c)
        for s, i, t, c in con.execute(
            "SELECT k.source, k.series_id, any_value(m.title), bool_or(coalesce(m.copyrighted, "
            "false)) OR any_value(k.cr) FROM _keys k LEFT JOIN series_meta m "
            "USING (source, series_id) GROUP BY ALL"
        ).fetchall()
    }
    units: dict[tuple[str, str], str] = {}
    if with_units:
        con.register(
            "_u",
            pa.table(
                {
                    "source": [k[0] for k in with_units],
                    "series_id": [k[1] for k in with_units],
                }
            ),
        )
        units = {
            (s, i): u or ""
            for s, i, u in con.execute(
                "SELECT s.source, s.series_id, any_value(s.units) FROM series s "
                "JOIN _u USING (source, series_id) GROUP BY ALL"
            ).fetchall()
        }
    fred_agencies = _fred_agencies(cfg)
    out = {}
    for src, sid, cr in keys:
        title, copyrighted = meta.get((src, sid), (None, cr))
        out[(src, sid)] = SeriesInfo(
            src,
            sid,
            title or sid,
            units.get((src, sid), ""),
            bool(copyrighted or cr),
            series_agencies(src, fred_agencies.get(sid) if src == "fred" else None),
        )
    return out


def _fred_agencies(cfg: dict[str, Any]) -> dict[str, list[str]]:
    root = _resolve(cfg["paths"]["processed_dir"]) / "_internal" / "fred_series_attrs"
    t = read_table(SERIES_ATTRS, root, source="fred")
    if not t.num_rows:
        return {}
    return dict(
        zip(t.column("series_id").to_pylist(), t.column("agencies").to_pylist(), strict=True)
    )


def growth_of(
    con: duckdb.DuckDBPyConnection, cfg: dict[str, Any], keys: list[tuple[str, str]]
) -> dict[tuple[str, str], GrowthSeries]:
    series = load_series(con, cfg, ids=keys)
    return {
        (p.series.source, p.series.series_id): GrowthSeries(
            p.item.growth, p.prep.last_index, p.series.grid
        )
        for p in prep_for("regime", series, cfg)
    }


# --- datos de texto ------------------------------------------------------------------------------


def term_text(object_id: str) -> str:
    return object_id.split("|", 1)[1] if "|" in object_id else object_id


def vocabulary(con: duckdb.DuckDBPyConnection, sources: list[str], n: int, seed: int) -> list[str]:
    """Muestra determinista (hash con semilla) de términos distintos de `term_freq`."""
    if not sources or n <= 0:
        return []
    rows = con.execute(
        "SELECT term FROM (SELECT DISTINCT term FROM term_freq WHERE list_contains(?, source)) "
        "ORDER BY hash(term || ?), term LIMIT ?",
        [sources, str(seed), n],
    ).fetchall()
    return [r[0] for r in rows]


def term_entities(
    con: duckdb.DuckDBPyConnection, signals: list[dict[str, Any]]
) -> dict[tuple[str, str], int]:
    """Máximo de `n_entities` del término en su ventana reciente (None si no se midió)."""
    if not signals:
        return {}
    con.register(
        "_terms",
        pa.table(
            {
                "source": [s["source"] for s in signals],
                "section": [s["object_id"].split("|", 1)[0] for s in signals],
                "term": [term_text(s["object_id"]) for s in signals],
            }
        ),
    )
    rows = con.execute(
        "SELECT t.source, t.section, t.term, f.period, f.n_entities FROM term_freq f "
        "JOIN _terms t USING (source, section, term) WHERE f.n_entities IS NOT NULL"
    ).fetchall()
    start = {(s["source"], s["object_id"]): s["window_start"] for s in signals}
    out: dict[tuple[str, str], int] = {}
    for src, section, term, period, n in rows:
        key = (src, f"{section}|{term}")
        if grid_start(period) >= start[key]:
            out[key] = max(out.get(key, 0), int(n))
    return out


# --- corrida -------------------------------------------------------------------------------------


def cluster_params(cfg: dict[str, Any]) -> ClusterParams:
    c = cfg["rank"]["cluster"]
    return ClusterParams(
        alpha=c["alpha"],
        threshold=c["threshold"],
        duplicate_rho=c["duplicate_rho"],
        duplicate_cos=c["duplicate_cos"],
        min_overlap=c["min_overlap"],
        max_series=c["max_series"],
    )


def link_params(cfg: dict[str, Any]) -> LinkParams:
    lk = cfg["rank"]["links"]
    return LinkParams(
        null_quantile=lk["null_quantile"],
        null_terms=lk["null_sample"],
        top_k=lk["top_k"],
        seed=lk["seed"],
    )


def _best_by_series(signals: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for s in signals:
        out[(s["source"], s["object_id"])].append(s)
    return out


def _overlaps(term: dict[str, Any], sigs: list[dict[str, Any]]) -> bool:
    return any(term["window_end"] >= s["window_start"] for s in sigs)


def rank_run(
    cfg: dict[str, Any],
    run_id: str,
    *,
    numeric_run: str | None = None,
    text_runs: dict[str, str] | None = None,
    embedder: Embedder | None = None,
    log: Any = None,
) -> dict[str, Any]:
    started = datetime.now(UTC).replace(microsecond=0)
    rk = cfg["rank"]
    numeric_run = numeric_run or latest_numeric_run(cfg)
    text_runs = text_runs if text_runs is not None else latest_text_runs(cfg)
    processed = _resolve(cfg["paths"]["processed_dir"])
    con = connect(processed)

    num_signals = [
        s
        for src in detect_sources(cfg)
        for s in (_signals(cfg, src, numeric_run) if numeric_run else [])
    ]
    txt_signals = [s for src, rid in sorted(text_runs.items()) for s in _signals(cfg, src, rid)]
    by_series = _best_by_series(num_signals)
    tested = tested_series(con, numeric_run)
    signal_keys = sorted(by_series)
    info = series_info(
        con,
        cfg,
        sorted(set(tested) | {(s, i, False) for s, i in signal_keys}),
        set(signal_keys),
    )
    inner = embedder or make_embedder(cfg, _resolve(rk["embeddings"]["cache_dir"]))
    emb = CachedEmbedder(inner, processed / "_internal" / "embeddings")

    # 1. Clusters de series con señal.
    cp = cluster_params(cfg)
    ordered = sorted(signal_keys, key=lambda k: (min(s["q_value"] for s in by_series[k]), k))
    head, tail = ordered[: cp.max_series], ordered[cp.max_series :]
    growth = growth_of(con, cfg, head)
    keyed = [k for k in head if k in growth]
    no_growth = [k for k in head if k not in growth]
    clusters: list[list[tuple[str, str]]] = []
    sim_to_rep: dict[tuple[str, str], float] = {}
    if keyed:
        vec = emb.embed([f"{info[k].title} | {info[k].units}" for k in keyed])
        rho = growth_correlation([growth[k] for k in keyed], cp.min_overlap)
        s = similarity(rho, vec @ vec.T, cp)
        labels = cluster_labels(s, cp.threshold)
        groups: dict[int, list[int]] = defaultdict(list)
        for i, lab in enumerate(labels):
            groups[int(lab)].append(i)
        for idx in groups.values():
            members = [keyed[i] for i in idx]
            rep = _representative(members, by_series, info)
            r = keyed.index(rep)
            for i in idx:
                sim_to_rep[keyed[i]] = float(s[i, r])
            clusters.append(_rep_first(members, rep))
    clusters += [[k] for k in no_growth + tail]
    cluster_of = {}
    cluster_rows = []
    cids = []
    for members in clusters:
        cid = cluster_id("c", [f"{s}|{i}" for s, i in members])
        cids.append(cid)
        for j, k in enumerate(members):
            cluster_of[k] = cid
            cluster_rows.append(
                {
                    "run_id": run_id,
                    "cluster_id": cid,
                    "source": k[0],
                    "object_id": k[1],
                    "role": "representante" if j == 0 else "miembro",
                    "similarity": sim_to_rep.get(k, 1.0 if j == 0 else None),
                }
            )

    # 2. Cruces término–serie (todas las series probadas) y término–término.
    lp = link_params(cfg)
    universe = sorted(info)
    terms = [(s["source"], s["object_id"]) for s in txt_signals]
    term_vec = emb.embed([term_text(o) for _, o in terms]) if terms else np.zeros((0, 1))
    link_rows: list[dict[str, Any]] = []
    thr_ts = thr_tt = None
    links_by_term: dict[tuple[str, str], list[tuple[tuple[str, str], float]]] = defaultdict(list)
    null_vec = np.zeros((0, 1))
    if terms:
        vocab = vocabulary(con, sorted(text_runs), lp.null_terms + len(terms), lp.seed)
        null = sample_null(vocab, {term_text(o) for _, o in terms}, lp)
        null_vec = emb.embed(null) if null else np.zeros((0, 1))
    if terms and universe:
        ser_vec = emb.embed([info[k].title for k in universe])
        thr_ts = null_threshold(null_vec, ser_vec, lp)
        method = f"{emb.name}|cos>q{lp.null_quantile}:{thr_ts:.4f}|top{lp.top_k}"
        for ti, sj, cos in top_links(term_vec, ser_vec, thr_ts, lp):
            src, sid = universe[sj]
            links_by_term[terms[ti]].append(((src, sid), cos))
            link_rows.append(
                {
                    "run_id": run_id,
                    "source": src,
                    "series_id": sid,
                    "text_source": terms[ti][0],
                    "term": terms[ti][1],
                    "score": cos,
                    "method": method,
                }
            )

    # 3. Evidencia y triangulación de clusters de series.
    tsig = {(s["source"], s["object_id"]): s for s in txt_signals}
    ents = term_entities(con, txt_signals)
    require_overlap = rk["links"]["require_overlap"]
    term_evidence_by_cluster: dict[str, dict[tuple[str, str], Evidence]] = defaultdict(dict)
    for tkey, linked in links_by_term.items():
        t = tsig[tkey]
        for skey, cos in linked:
            cid = cluster_of.get(skey)
            if cid is None:
                continue
            if require_overlap and not _overlaps(t, by_series[skey]):
                continue
            prev = term_evidence_by_cluster[cid].get(tkey)
            if prev is None or cos > (prev.link_score or 0):
                term_evidence_by_cluster[cid][tkey] = _term_evidence(
                    t, ents.get(tkey), cos, f"{skey[0]}|{skey[1]}"
                )
    min_sources = rk["min_sources"]
    candidates: list[Candidate] = []
    for cid, members in zip(cids, clusters, strict=True):
        ev = [_series_evidence(k, by_series[k], info[k]) for k in members]
        ev += sorted(
            term_evidence_by_cluster.get(cid, {}).values(), key=lambda e: (e.source, e.object_id)
        )
        candidates.append(assess(Candidate(cid, "series", ev, len(members)), min_sources))

    # 4. Candidatos solo de texto: términos emergentes parecidos en distintas fuentes de texto.
    term_clusters: list[list[int]] = []
    if rk["text_only"]["enabled"] and len(terms) >= 2:
        tt = term_vec @ term_vec.T
        thr_tt = max(null_threshold(null_vec, term_vec, lp), rk["text_only"]["min_cos"])
        labels = cluster_labels(np.clip(tt, 0, 1), thr_tt)
        groups = defaultdict(list)
        for i, lab in enumerate(labels):
            groups[int(lab)].append(i)
        for idx in groups.values():
            if len({terms[i][0] for i in idx}) < 2:
                continue
            term_clusters.append(idx)
            members = [terms[i] for i in idx]
            cid = cluster_id("t", [f"{s}|{o}" for s, o in members])
            for j, i in enumerate(sorted(idx, key=lambda i: (tsig[terms[i]]["q_value"], terms[i]))):
                cluster_rows.append(
                    {
                        "run_id": run_id,
                        "cluster_id": cid,
                        "source": terms[i][0],
                        "object_id": terms[i][1],
                        "role": "representante" if j == 0 else "miembro",
                        "similarity": None,
                    }
                )
            ev = [_term_evidence(tsig[k], ents.get(k), None, None) for k in sorted(members)]
            candidates.append(assess(Candidate(cid, "text", ev, len(members)), min_sources))

    score(candidates, rk["weights"], cfg["detect"]["detectors"], rk["max_candidates"])
    con.close()

    # 5. Escritura.
    cand_rows, ev_rows = [], []
    for c in candidates:
        cand_rows.append(
            {
                "run_id": run_id,
                "candidate_id": c.candidate_id,
                "kind": c.kind,
                "status": c.status,
                "rank": c.rank,
                "score": c.score,
                "n_sources": c.n_sources,
                "n_sources_public": c.n_sources_public,
                "sources_json": json.dumps([sorted(g) for g in c.groups]),
                "components_json": json.dumps(c.components) if c.components else None,
                "n_members": c.n_members,
                "n_terms": sum(e.kind == "term" for e in c.evidence),
                "published": c.published,
            }
        )
        for e in c.evidence:
            ev_rows.append(
                {
                    "run_id": run_id,
                    "candidate_id": c.candidate_id,
                    "item_kind": e.kind,
                    "source": e.source,
                    "object_id": e.object_id,
                    "detectors": ",".join(e.detectors),
                    "q_value": e.q_value,
                    "effect_size": e.effect_size,
                    "agencies_json": json.dumps(sorted(e.agencies)),
                    "public": e.public,
                    "window_start": e.window_start,
                    "window_end": e.window_end,
                    "link_score": e.link_score,
                    "linked_series": e.linked_series,
                    "n_entities": e.n_entities,
                }
            )
    fred_names = [a for v in _fred_agencies(cfg).values() for a in (v or [])]
    n_cand = sum(c.status == "candidato" for c in candidates)
    summary = {
        "run_id": run_id,
        "numeric_run_id": numeric_run,
        "text_run_ids": text_runs,
        "n_signal_series": len(signal_keys),
        "n_signal_terms": len(terms),
        "n_clusters": len(clusters),
        "n_clusters_multi": sum(len(m) > 1 for m in clusters),
        "n_unclustered_over_cap": len(tail),
        "n_term_clusters": len(term_clusters),
        "n_links": len(link_rows),
        "link_threshold": thr_ts,
        "n_null_terms": int(null_vec.shape[0]) if terms else 0,
        "term_term_threshold": thr_tt,
        "n_candidates": n_cand,
        "n_single_source": sum(c.status == "una_fuente" for c in candidates),
        "n_over_cap": sum(c.status == "fuera_del_tope" for c in candidates),
        "n_published": sum(c.published for c in candidates),
        "embeddings": {"model": emb.name, "computed": emb.computed, "reused": emb.reused},
        "adapter_agencies_unmatched": unmatched_adapter_agencies(fred_names),
    }
    run_row = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": datetime.now(UTC).replace(microsecond=0),
        "numeric_run_id": numeric_run,
        "text_run_ids_json": json.dumps(text_runs, sort_keys=True),
        "n_signal_series": len(signal_keys),
        "n_signal_terms": len(terms),
        "n_clusters": len(clusters),
        "n_term_clusters": len(term_clusters),
        "n_links": len(link_rows),
        "n_candidates": n_cand,
        "n_single_source": summary["n_single_source"],
        "n_published": summary["n_published"],
        "params_json": json.dumps({"rank": rk, "summary": summary}, sort_keys=True, default=str),
    }
    _write("clusters", cluster_rows, cfg)
    _write("series_term_links", link_rows, cfg)
    _write("rank_candidates", cand_rows, cfg)
    _write("rank_evidence", ev_rows, cfg)
    _write("rank_runs", [run_row], cfg)
    if log is not None:
        log.info("ranking", extra={"fields": summary})
    return summary


def _write(table: str, rows: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    spec = RANK_TABLES[table]
    t = pa.Table.from_pylist(rows, schema=spec.schema)
    upsert(
        t, spec, rank_root(cfg, table), source=PARTITION, n_buckets=N_BUCKETS, bucket_col="run_id"
    )


def _representative(
    members: list[tuple[str, str]],
    by_series: dict[tuple[str, str], list[dict[str, Any]]],
    info: dict[tuple[str, str], SeriesInfo],
) -> tuple[str, str]:
    return min(
        members,
        key=lambda k: (info[k].copyrighted, min(s["q_value"] for s in by_series[k]), k),
    )


def _rep_first(members: list[tuple[str, str]], rep: tuple[str, str]) -> list[tuple[str, str]]:
    return [rep] + sorted(m for m in members if m != rep)


def _series_evidence(
    key: tuple[str, str], sigs: list[dict[str, Any]], info: SeriesInfo
) -> Evidence:
    best = min(sigs, key=lambda s: s["q_value"])
    return Evidence(
        kind="series",
        source=key[0],
        object_id=key[1],
        agencies=info.agencies,
        q_value=float(best["q_value"]),
        effect_size=float(best["effect_size"]),
        detectors=tuple(sorted({s["detector"] for s in sigs})),
        public=not info.copyrighted,
        window_start=min(s["window_start"] for s in sigs),
        window_end=max(s["window_end"] for s in sigs),
    )


def _term_evidence(
    t: dict[str, Any], n_entities: int | None, cos: float | None, linked: str | None
) -> Evidence:
    return Evidence(
        kind="term",
        source=t["source"],
        object_id=t["object_id"],
        agencies=text_agencies(t["source"]),
        q_value=float(t["q_value"]),
        effect_size=float(t["effect_size"]),
        detectors=(t["detector"],),
        public=True,
        window_start=t["window_start"],
        window_end=t["window_end"],
        link_score=cos,
        linked_series=linked,
        n_entities=n_entities,
    )


def latest_rank_run(cfg: dict[str, Any]) -> dict[str, Any] | None:
    rows = read_table(RANK_RUNS, rank_root(cfg, "rank_runs"), source=PARTITION).to_pylist()
    return max(rows, key=lambda r: (r["finished_at"], r["run_id"])) if rows else None


# --- CLI -----------------------------------------------------------------------------------------


def _parse_text_runs(value: str) -> dict[str, str]:
    out = {}
    for part in value.split(","):
        if part:
            src, rid = part.split("=", 1)
            out[src] = rid
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.rank")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="clusters, cruces, triangulación y score")
    r.add_argument("--numeric-run", default=None)
    r.add_argument("--text-runs", type=_parse_text_runs, default=None, help="edgar=ID,...")
    args = parser.parse_args(argv)
    cfg = load_config()
    log = get_logger("rank", cfg["logging"]["level"])
    with run(["rank"], _resolve(cfg["report"]["runs_file"])) as record:
        with record.stage("rank"):
            summary = rank_run(
                cfg,
                record.run_id,
                numeric_run=args.numeric_run,
                text_runs=args.text_runs,
                log=log,
            )
        record.n_series["signal_series"] = summary["n_signal_series"]
        record.details = {"command": "rank run", "summary": summary}
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
