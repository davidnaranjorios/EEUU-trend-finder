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
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa

from src.catalog import connect
from src.config import REPO_ROOT, load_config
from src.detect.run import (
    _resolve,
    detect_sources,
    load_series,
    prep_for,
    reference_keys,
    table_root,
)
from src.detect.text import DETECTOR as TEXT_DETECTOR
from src.detect.text import grid_start
from src.ingest.fred import SERIES_ATTRS
from src.ingest.store import read_table, upsert
from src.log import get_logger, run
from src.rank import inflation_effect as infl_mod
from src.rank import naics as naics_mod
from src.rank import origin as origin_mod
from src.rank import rate_effect as rate_mod
from src.rank.agencies import series_agencies, text_agencies, unmatched_adapter_agencies
from src.rank.cluster import (
    ClusterParams,
    GrowthSeries,
    cluster_id,
    cluster_labels,
    growth_correlation,
    similarity,
)
from src.rank.comove import (
    ComoveParams,
    coarser,
    comove_joint,
    max_overlap,
    on_axis,
    series_grid_growth,
    term_annual_change,
)
from src.rank.context import select_related
from src.rank.embed import CachedEmbedder, Embedder, make_embedder
from src.rank.links import LinkParams, null_threshold, sample_null, top_links
from src.rank.phenomena import PhenomenaParams, group_phenomena
from src.rank.triangulate import Candidate, Evidence, assess, score
from src.schemas import (
    CLUSTERS,
    DETECTOR_RUNS,
    NAICS_LINKS,
    RANK_CANDIDATES,
    RANK_CONTEXT,
    RANK_EVIDENCE,
    RANK_ORIGIN,
    RANK_PHENOMENA,
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
    "rank_origin": RANK_ORIGIN,
    "rank_runs": RANK_RUNS,
    "rank_context": RANK_CONTEXT,
    "naics_links": NAICS_LINKS,
    "rank_phenomena": RANK_PHENOMENA,
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
    agencies: frozenset[str]  # identidad de fuente para triangular: el ORIGEN (ADR 0010)
    chain: tuple[str, ...] = ()  # cadena de origen, raíz primero (vacía si no hay regla)
    rule_id: str | None = None
    duplicates: tuple[str, ...] = ()  # "fuente|serie" de posibles copias de otra agencia
    duplicate_rho: float | None = None

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
    fred_releases = _fred_releases(cfg)
    rules = origin_mod.load_rules(_origin_path(cfg))
    # Las unidades solo se leen de las series con señal; las reglas por unidades las usan si hay.
    out = {}
    for src, sid, cr in keys:
        title, copyrighted = meta.get((src, sid), (None, cr))
        agencies = series_agencies(src, fred_agencies.get(sid) if src == "fred" else None)
        org = origin_mod.resolve_origin(
            rules,
            source=src,
            series_id=sid,
            agencies=agencies,
            release_ids=fred_releases.get(sid, ()) if src == "fred" else (),
            units=units.get((src, sid), ""),
            title=title or "",
        )
        out[(src, sid)] = SeriesInfo(
            src,
            sid,
            title or sid,
            units.get((src, sid), ""),
            bool(copyrighted or cr),
            org.keys,
            org.chain,
            org.rule_id,
        )
    return out


def _origin_path(cfg: dict[str, Any]) -> Any:
    p = cfg["rank"].get("origin", {}).get("table")
    return _resolve(p) if p else None


def _fred_releases(cfg: dict[str, Any]) -> dict[str, list[int]]:
    root = _resolve(cfg["paths"]["processed_dir"]) / "_internal" / "fred_series_attrs"
    t = read_table(SERIES_ATTRS, root, source="fred")
    if not t.num_rows:
        return {}
    return dict(
        zip(t.column("series_id").to_pylist(), t.column("release_ids").to_pylist(), strict=True)
    )


def _flag_duplicates(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    keyed: list[tuple[str, str]],
    rho: Any,
    info: dict[tuple[str, str], SeriesInfo],
) -> None:
    """Marca "posible mismo origen" entre agencias distintas y unifica su identidad (ADR 0010)."""
    oc = cfg["rank"]["origin"]
    levels = {
        (p.source, p.series_id): (
            p.grid,
            {p.first_index + i: float(v) for i, v in enumerate(p.level)},
        )
        for p in load_series(con, cfg, ids=keyed)
    }
    ident = {k: info[k].agencies for k in keyed}
    pairs = origin_mod.find_duplicates(
        keyed,
        rho,
        levels,
        ident,
        origin_mod.DuplicateParams(oc["duplicate_rho"], oc["level_tol"], oc["min_overlap"]),
    )
    origin_mod.merge_identities(ident, pairs)
    for a, b, r in pairs:
        for x, y in ((a, b), (b, a)):
            info[x].duplicates = (*info[x].duplicates, f"{y[0]}|{y[1]}")
            info[x].duplicate_rho = max(r, info[x].duplicate_rho or 0.0)
    for k in keyed:
        info[k].agencies = ident[k]


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
            p.item.growth, p.prep.last_index, p.series.grid, p.item.scale, p.item.persistence
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


def term_industries(
    con: duckdb.DuckDBPyConnection, signals: list[dict[str, Any]]
) -> dict[tuple[str, str], float]:
    """Mediana de `n_industries` (SIC) del término en su ventana (ausente si no se midió)."""
    if not signals:
        return {}
    con.register(
        "_iterms",
        pa.table(
            {
                "source": [s["source"] for s in signals],
                "section": [s["object_id"].split("|", 1)[0] for s in signals],
                "term": [term_text(s["object_id"]) for s in signals],
            }
        ),
    )
    cols = {r[0] for r in con.execute("DESCRIBE term_freq").fetchall()}
    if "n_industries" not in cols:
        # Particiones anteriores al esquema 4 (ADR 0011): aún no medido en ninguna.
        return {}
    rows = con.execute(
        "SELECT t.source, t.section, t.term, f.period, f.n_industries FROM term_freq f "
        "JOIN _iterms t USING (source, section, term) WHERE f.n_industries IS NOT NULL"
    ).fetchall()
    start = {(s["source"], s["object_id"]): s["window_start"] for s in signals}
    vals: dict[tuple[str, str], list[int]] = defaultdict(list)
    for src, section, term, period, n in rows:
        key = (src, f"{section}|{term}")
        if grid_start(period) >= start[key]:
            vals[key].append(int(n))
    return {k: float(np.median(v)) for k, v in vals.items()}


def meta_texts(
    con: duckdb.DuckDBPyConnection, keys: list[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """Título y ruta de categoría de cada serie (donde puede venir un código NAICS explícito)."""
    if not keys:
        return {}
    con.register(
        "_nkeys", pa.table({"source": [k[0] for k in keys], "series_id": [k[1] for k in keys]})
    )
    return {
        (s, i): f"{t or ''} | {c or ''}"
        for s, i, t, c in con.execute(
            "SELECT k.source, k.series_id, any_value(m.title), any_value(m.category_path) "
            "FROM _nkeys k JOIN series_meta m USING (source, series_id) GROUP BY ALL"
        ).fetchall()
    }


def term_sics(
    con: duckdb.DuckDBPyConnection, signals: list[dict[str, Any]]
) -> dict[tuple[str, str], dict[str, int]]:
    """SIC (4 dígitos) de las empresas que mencionan cada término en su ventana, por documentos.

    Vacío mientras las particiones de `term_freq` no traigan `sic_top_json` (esquema < 5):
    el anclaje de esos términos queda pendiente de datos y no se inventa.
    """
    if not signals:
        return {}
    cols = {r[0] for r in con.execute("DESCRIBE term_freq").fetchall()}
    if "sic_top_json" not in cols:
        return {}
    con.register(
        "_sterms",
        pa.table(
            {
                "source": [s["source"] for s in signals],
                "section": [s["object_id"].split("|", 1)[0] for s in signals],
                "term": [term_text(s["object_id"]) for s in signals],
            }
        ),
    )
    rows = con.execute(
        "SELECT t.source, t.section, t.term, f.period, f.sic_top_json FROM term_freq f "
        "JOIN _sterms t USING (source, section, term) WHERE f.sic_top_json IS NOT NULL"
    ).fetchall()
    start = {(s["source"], s["object_id"]): s["window_start"] for s in signals}
    acc: dict[tuple[str, str], list[str]] = defaultdict(list)
    for src, section, term, period, payload in rows:
        key = (src, f"{section}|{term}")
        if key in start and grid_start(period) >= start[key]:
            acc[key].append(payload)
    return {k: naics_mod.parse_sic_top(v) for k, v in acc.items()}


def term_base_share(
    con: duckdb.DuckDBPyConnection, signals: list[dict[str, Any]]
) -> dict[tuple[str, str], float]:
    """Prevalencia base: documentos con el término / documentos, en toda la historia anterior a
    su ventana (la que se usa para penalizar a las palabras comunes)."""
    if not signals:
        return {}
    start = {(s["source"], s["object_id"]): s["window_start"] for s in signals}
    con.register(
        "_bterms",
        pa.table(
            {
                "source": [s["source"] for s in signals],
                "section": [s["object_id"].split("|", 1)[0] for s in signals],
                "term": [term_text(s["object_id"]) for s in signals],
            }
        ),
    )
    rows = con.execute(
        "SELECT t.source, t.section, t.term, f.period, f.doc_count, f.total_docs FROM term_freq f "
        "JOIN _bterms t USING (source, section, term)"
    ).fetchall()
    num: dict[tuple[str, str], float] = defaultdict(float)
    den: dict[tuple[str, str], float] = defaultdict(float)
    for src, section, term, period, c, n in rows:
        key = (src, f"{section}|{term}")
        if grid_start(period) < start[key]:
            num[key] += c
            den[key] += n
    return {k: num[k] / den[k] for k in num if den[k] > 0}


# Índice de periodo de `term_freq` en cada grilla del test de co-movimiento: año (A) o
# año·4 + trimestre (Q). Los periodos de EDGAR son trimestres ('2024Q1') y los del Federal
# Register meses ('2024-05'); ambos se agregan a la grilla sin interpolar.
GRID_SQL = {
    "A": "substr(period, 1, 4)::INT",
    # Un periodo más grueso que la grilla (p. ej. '2024' en Q) no se puede repartir: queda NULL
    # y se descarta (no se interpola).
    "Q": (
        "CASE WHEN period LIKE '____Q_' THEN substr(period, 1, 4)::INT * 4 + "
        "substr(period, 6, 1)::INT - 1 "
        "WHEN period LIKE '____-__' THEN substr(period, 1, 4)::INT * 4 + "
        "(substr(period, 6, 2)::INT - 1) // 3 END"
    ),
}


def term_grid_counts(
    con: duckdb.DuckDBPyConnection,
    signals: list[dict[str, Any]],
    min_doc_count: int,
    grids: set[str],
) -> dict[str, dict[tuple[str, str], dict[int, tuple[float, float]]]]:
    """(c, n) por periodo de cada grilla de `grids` (A, Q) de cada término con señal.

    Solo periodos completos: con todos los subperiodos de `term_freq` de la sección (4 trimestres
    por año; 3 meses por trimestre). Un subperiodo sin fila del término se imputa en el tope de la
    censura. El término se agrega, nunca se interpola."""
    if not signals:
        return {g: {} for g in grids}
    srcs = sorted({s["source"] for s in signals})
    con.register(
        "_cterms",
        pa.table(
            {
                "source": [s["source"] for s in signals],
                "section": [s["object_id"].split("|", 1)[0] for s in signals],
                "term": [term_text(s["object_id"]) for s in signals],
            }
        ),
    )
    result: dict[str, dict[tuple[str, str], dict[int, tuple[float, float]]]] = {}
    for grid in sorted(grids):
        expr = GRID_SQL[grid]
        tot = con.execute(
            f"SELECT source, section, {expr} g, count(*) n_sub, sum(total_docs) n "
            "FROM (SELECT DISTINCT source, section, period, total_docs FROM term_freq "
            "WHERE list_contains(?, source)) GROUP BY ALL",
            [srcs],
        ).fetchall()
        full: dict[tuple[str, str], dict[int, tuple[int, float]]] = {}
        for src, sec, g, n_sub, n in tot:
            if g is None:
                continue
            full.setdefault((src, sec), {})[g] = (n_sub, n)
        need = {k: max(v[0] for v in d.values()) for k, d in full.items()}
        rows = con.execute(
            f"SELECT t.source, t.section, t.term, {expr}, sum(f.doc_count), count(*) "
            "FROM term_freq f JOIN _cterms t USING (source, section, term) GROUP BY ALL"
        ).fetchall()
        out: dict[tuple[str, str], dict[int, tuple[float, float]]] = {}
        for src, sec, term, g, c, n_present in rows:
            if g is None or (src, sec) not in full:
                continue
            n_sub, n = full[(src, sec)][g]
            if n_sub != need[(src, sec)]:
                continue
            c = min(c + (n_sub - n_present) * (min_doc_count - 1), n)
            out.setdefault((src, f"{sec}|{term}"), {})[g] = (float(c), float(n))
        result[grid] = out
    return result


def text_grids(cfg: dict[str, Any], sources: set[str]) -> dict[str, str]:
    """Grilla del término por fuente de texto (`rank.links.comove.text_grid`; por defecto A)."""
    tg = cfg["rank"]["links"]["comove"].get("text_grid") or {}
    return {s: tg.get(s, "A") for s in sources}


def _lag_for(max_lag: Any, grid: str) -> int:
    return int(max_lag[grid] if isinstance(max_lag, dict) else max_lag)


def comove_validate(
    cfg: dict[str, Any],
    counts: dict[str, dict[tuple[str, str], dict[int, tuple[float, float]]]],
    growth: dict[tuple[str, str], GrowthSeries],
    pairs: list[tuple[tuple[str, str], tuple[str, str]]],
    stats: dict[str, int] | None = None,
) -> dict[tuple[tuple[str, str], tuple[str, str]], dict[str, Any]]:
    """Prueba de co-movimiento de los pares sugeridos por títulos: una sola familia BH.

    Alineación de frecuencias: cada par se compara en la grilla MÁS GRUESA entre la del término
    (`rank.links.comove.text_grid`: Federal Register trimestral, EDGAR anual) y la nativa de la
    serie (M, Q o A). La serie se agrega a esa grilla sumando su crecimiento log de los periodos
    completos; el término nunca se interpola (una serie anual con un término trimestral se prueba
    en anual, con el término agregado a años). `counts` trae los conteos del término por grilla.
    Los pares con menos periodos comunes que el mínimo de su grilla (`min_overlap_by_grid`) no son
    pruebas: no entran a la familia (inflarían el denominador de BH sin poder), pero se cuentan en
    `stats`. Los índices de bloques son los mismos dentro de cada grilla y todos los p se corrigen
    juntos (una sola familia BH). `stats` (opcional) recibe los conteos por etapa del embudo."""
    cm = cfg["rank"]["links"]["comove"]
    st = stats if stats is not None else {}
    st.update({"pairs_in": len(pairs), "with_data": 0, "enough_overlap": 0, "tested": 0, "bh": 0})
    tgrid = text_grids(cfg, {t[0] for t, _ in pairs})
    by_grid = cm.get("min_overlap_by_grid", {})
    prepared: dict[str, list[tuple[tuple[str, str], tuple[str, str], dict, dict]]] = defaultdict(
        list
    )
    for t, s in pairs:
        if s not in growth:
            continue
        cg = coarser(tgrid[t[0]], growth[s].grid)
        ch = counts.get(cg, {}).get(t)
        if ch is None:
            continue
        g = growth[s]
        prepared[cg].append(
            (t, s, term_annual_change(ch), series_grid_growth(g.growth, g.last_index, g.grid, cg))
        )
        st["with_data"] += 1
    if not prepared:
        return {}
    groups, keys = [], []
    for cg in sorted(prepared):
        rows = prepared[cg]
        years = sorted({y for _, _, tc, sg in rows for y in (*tc, *sg)})
        axis = list(range(years[0], years[-1] + 1))
        lag = _lag_for(cm["max_lag"], cg)
        params = ComoveParams(
            lag, cm["min_overlap"], cm["n_resamples"], cm["block_length"], cm["seed"]
        )
        mo = int(by_grid.get(cg, cm["min_overlap"]))
        vecs, mins, keep = [], [], []
        for t, s, tc, sg in rows:
            a, b = on_axis(tc, axis), on_axis(sg, axis)
            if max_overlap(a, b, lag) >= mo:
                vecs.append((a, b))
                mins.append(mo)
                keep.append((t, s))
        st[f"tested_grid_{cg}"] = len(keep)
        if keep:
            groups.append((vecs, params, mins))
            keys.append((cg, keep))
    st["enough_overlap"] = st["tested"] = sum(len(k) for _, k in keys)
    results = comove_joint(groups)
    q_link = float(cfg["fdr"]["q"])
    out: dict[tuple[tuple[str, str], tuple[str, str]], dict[str, Any]] = {}
    for (cg, keep), res in zip(keys, results, strict=True):
        for pair, r in zip(keep, res, strict=True):
            out[pair] = {**r, "grid": cg}
    st["bh"] = sum(r["q_bh"] <= q_link for r in out.values())
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


def rate_effects(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    clusters: list[list[tuple[str, str]]],
    cids: list[str],
    growth: dict[tuple[str, str], GrowthSeries],
) -> tuple[dict[str, rate_mod.RateResult], dict[str, Any]]:
    """Etiqueta de efecto de tasas por cluster de series (ver `rate_effect.py`).

    La serie del cluster es su representante (o el primer miembro con crecimiento preparado).
    Sin la serie de tasa descargada no se evalúa nada y el resumen lo dice (no se inventa)."""
    ref = (cfg["context"]["reference_series"] or {}).get("short_rate")
    summary: dict[str, Any] = {"reference": ref, "available": False}
    rows = (
        con.execute(
            "SELECT date, value FROM series WHERE source = 'fred' AND series_id = ? "
            "AND vintage_date IS NULL AND value IS NOT NULL ORDER BY date",
            [ref],
        ).fetchall()
        if ref
        else []
    )
    if not rows:
        return {}, summary
    levels = {d.year * 12 + d.month - 1: float(v) for d, v in rows}
    items: dict[str, tuple[np.ndarray, int, str, float, int]] = {}
    for cid, members in zip(cids, clusters, strict=True):
        g = next((growth[k] for k in members if k in growth), None)
        if g is not None:
            items[cid] = (g.growth, g.last_index, g.grid, g.scale, g.persistence)
    dr = {grid: rate_mod.rate_changes(levels, grid) for grid in ("M", "Q", "A")}
    lv = {grid: rate_mod.rate_levels(levels, grid) for grid in ("M", "Q", "A")}
    params = rate_mod.params_from(cfg)
    res = rate_mod.evaluate(items, dr, params, lv)
    # Por qué no se evaluó cada cluster (sin crecimiento preparado: ni el detector lo pudo usar).
    reasons = Counter(r.reason for r in res.values() if not r.evaluated and r.reason)
    reasons["sin_crecimiento"] = len(clusters) - len(items)
    summary.update(
        {
            "available": True,
            "include_level": params.include_level,
            "n_clusters": len(clusters),
            "n_evaluated": sum(r.evaluated for r in res.values()),
            "n_marked": sum(r.rate_effect for r in res.values()),
            "not_evaluated_by_reason": {k: v for k, v in sorted(reasons.items()) if v},
        }
    )
    return res, summary


def inflation_effects(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    clusters: list[list[tuple[str, str]]],
    cids: list[str],
    growth: dict[tuple[str, str], GrowthSeries],
) -> tuple[dict[str, rate_mod.RateResult], dict[str, Any]]:
    """Etiqueta de efecto inflación por cluster (ver `inflation_effect.py`).

    Mismo criterio que `rate_effects`; el regresor es la inflación del deflactor aprobado
    (`context.reference_series.deflator`). Sin el deflactor descargado no se evalúa nada."""
    ref = (cfg["context"]["reference_series"] or {}).get("deflator")
    summary: dict[str, Any] = {"reference": ref, "available": False}
    rows = (
        con.execute(
            "SELECT date, value FROM series WHERE source = 'fred' AND series_id = ? "
            "AND vintage_date IS NULL AND value IS NOT NULL ORDER BY date",
            [ref],
        ).fetchall()
        if ref
        else []
    )
    if not rows:
        return {}, summary
    levels = {d.year * 12 + d.month - 1: float(v) for d, v in rows}
    items: dict[str, tuple[np.ndarray, int, str, float, int]] = {}
    for cid, members in zip(cids, clusters, strict=True):
        g = next((growth[k] for k in members if k in growth), None)
        if g is not None:
            items[cid] = (g.growth, g.last_index, g.grid, g.scale, g.persistence)
    res = infl_mod.evaluate(items, levels, infl_mod.params_from(cfg))
    reasons = Counter(r.reason for r in res.values() if not r.evaluated and r.reason)
    reasons["sin_crecimiento"] = len(clusters) - len(items)
    summary.update(
        {
            "available": True,
            "n_clusters": len(clusters),
            "n_evaluated": sum(r.evaluated for r in res.values()),
            "n_marked": sum(r.rate_effect for r in res.values()),
            "not_evaluated_by_reason": {k: v for k, v in sorted(reasons.items()) if v},
        }
    )
    return res, summary


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
    refs = reference_keys(cfg)  # series de referencia metodológica: nunca al ranking
    num_signals = [x for x in num_signals if (x["source"], x["object_id"]) not in refs]
    by_series = _best_by_series(num_signals)
    tested = [t for t in tested_series(con, numeric_run) if (t[0], t[1]) not in refs]
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
        _flag_duplicates(con, cfg, keyed, rho, info)
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
    # Etiqueta "efecto de tasas" por cluster (descriptiva: nunca filtra ni cambia el score).
    all_cids = [cluster_id("c", [f"{s}|{i}" for s, i in m]) for m in clusters]
    rate_by_cid, rate_summary = rate_effects(con, cfg, clusters, all_cids, growth)
    infl_by_cid, infl_summary = inflation_effects(con, cfg, clusters, all_cids, growth)
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
                    "rate_effect": rate_by_cid[cid].rate_effect if cid in rate_by_cid else None,
                    "rate_share": rate_by_cid[cid].rate_share if cid in rate_by_cid else None,
                    "inflation_effect": infl_by_cid[cid].rate_effect
                    if cid in infl_by_cid
                    else None,
                    "inflation_share": infl_by_cid[cid].rate_share if cid in infl_by_cid else None,
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
    funnel: dict[str, Any] = {"n_terms": len(terms), "n_signal_series": len(signal_keys)}
    seen_pairs: set[tuple[tuple[str, str], tuple[str, str]]] = set()

    def add_links(keys: list[tuple[str, str]], vecs: np.ndarray, thr: float, method: str) -> int:
        n = 0
        for ti, sj, cos in top_links(term_vec, vecs, thr, lp):
            skey = keys[sj]
            n += 1
            if (terms[ti], skey) in seen_pairs:
                continue
            seen_pairs.add((terms[ti], skey))
            links_by_term[terms[ti]].append((skey, cos))
            link_rows.append(
                {
                    "run_id": run_id,
                    "source": skey[0],
                    "series_id": skey[1],
                    "text_source": terms[ti][0],
                    "term": terms[ti][1],
                    "score": cos,
                    "method": method,
                    "comove_q": None,
                    "comove_lag": None,
                    "validated": False,
                }
            )
        return n

    if terms and universe:
        ser_vec = emb.embed([info[k].title for k in universe])
        thr_ts = null_threshold(null_vec, ser_vec, lp)
        method = f"{emb.name}|cos>q{lp.null_quantile}:{thr_ts:.4f}|top{lp.top_k}"
        funnel["pairs_similarity_universe"] = add_links(universe, ser_vec, thr_ts, method)
        funnel["pairs_similarity_on_signal_series"] = sum(
            1 for _, sk in seen_pairs if sk in by_series
        )
        # Solo una serie con señal puede ser un segundo respaldo del cluster: el top_k sobre las
        # ~400 mil series del universo se llena de series sin señal. Segundo pase contra las series
        # con señal, con su propio nulo por término (mismo cuantil, mismos términos del nulo).
        pos = {k: i for i, k in enumerate(universe)}
        sig_universe = [k for k in universe if k in by_series]
        if sig_universe:
            sig_vec = ser_vec[[pos[k] for k in sig_universe]]
            thr_sig = null_threshold(null_vec, sig_vec, lp)
            funnel["link_threshold_signal_series"] = thr_sig
            before = len(seen_pairs)
            add_links(
                sig_universe,
                sig_vec,
                thr_sig,
                f"{emb.name}|cos>q{lp.null_quantile}:{thr_sig:.4f}|top{lp.top_k}|series_con_senal",
            )
            funnel["pairs_added_signal_pass"] = len(seen_pairs) - before
    funnel["pairs_similarity_total"] = len(seen_pairs)

    # 3. Evidencia y triangulación de clusters de series.
    tsig = {(s["source"], s["object_id"]): s for s in txt_signals}
    ents = term_entities(con, txt_signals)
    inds = term_industries(con, txt_signals)
    require_overlap = rk["links"]["require_overlap"]
    q_link = float(cfg["fdr"]["q"])
    suggested = [
        (tkey, skey)
        for tkey, linked in links_by_term.items()
        for skey, _ in linked
        if skey in cluster_of and (not require_overlap or _overlaps(tsig[tkey], by_series[skey]))
    ]
    funnel["pairs_in_cluster_universe"] = sum(
        1 for t, linked in links_by_term.items() for sk, _ in linked if sk in cluster_of
    )
    funnel["pairs_window_overlap"] = len(suggested)
    cstats: dict[str, int] = {}
    comove = comove_validate(
        cfg,
        term_grid_counts(
            con,
            txt_signals,
            cfg["text"]["min_doc_count"],
            {"A", *text_grids(cfg, {x["source"] for x in txt_signals}).values()},
        ),
        growth,
        suggested,
        cstats,
    )
    funnel.update({f"comove_{k}": v for k, v in cstats.items() if k != "pairs_in"})
    funnel["terms_validated"] = len({t for (t, _), r in comove.items() if r["q_bh"] <= q_link})
    for lr in link_rows:
        res = comove.get(((lr["text_source"], lr["term"]), (lr["source"], lr["series_id"])))
        if res is not None:
            lr["comove_q"], lr["comove_lag"] = res["q_bh"], res["lag"]
            lr["validated"] = res["q_bh"] <= q_link
        else:
            lr["validated"] = False
    shares = term_base_share(con, txt_signals)
    term_evidence_by_cluster: dict[str, dict[tuple[str, str], Evidence]] = defaultdict(dict)
    for tkey, linked in links_by_term.items():
        t = tsig[tkey]
        for skey, cos in linked:
            cid = cluster_of.get(skey)
            if cid is None:
                continue
            if require_overlap and not _overlaps(t, by_series[skey]):
                continue
            res = comove.get((tkey, skey))
            ok = bool(res and res["q_bh"] <= q_link)
            rank_key = (ok, cos)
            prev = term_evidence_by_cluster[cid].get(tkey)
            if prev is None or rank_key > (bool(prev.link_validated), prev.link_score or 0):
                ev = _term_evidence(
                    t, ents.get(tkey), cos, f"{skey[0]}|{skey[1]}", validated=ok, comove=res
                )
                ev.base_share = shares.get(tkey)
                ev.n_industries = inds.get(tkey)
                term_evidence_by_cluster[cid][tkey] = ev
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

    # 4b. Anclaje a industria NAICS (ADR 0012).
    tables = naics_mod.load_tables(cfg, REPO_ROOT)
    own = naics_mod.series_links(meta_texts(con, signal_keys), tables)
    by_origin = naics_mod.inherit_by_origin(
        {k: [tuple(d.split("|", 1)) for d in info[k].duplicates] for k in signal_keys if k in info},
        own,
    )
    own = {**own, **by_origin}
    naics_links = {
        **own,
        **naics_mod.inherit_in_clusters(clusters, own, float(rk["naics"]["cluster_majority"])),
        **naics_mod.term_links(term_sics(con, txt_signals), tables),
    }
    naics_cov = naics_mod.coverage(
        [*signal_keys, *terms], naics_links, rk["naics"]["not_applicable_sources"]
    )
    score(candidates, rk["weights"], cfg["detect"]["detectors"], rk["max_candidates"])
    # 4c. Fenómenos: clusters de series con NAICS compartido, misma dirección y ventanas solapadas.
    pp = rk["phenomena"]
    phenomena = group_phenomena(
        candidates,
        {cid: m for cid, m in zip(cids, clusters, strict=True)},
        naics_links,
        PhenomenaParams(pp["naics_digits"], pp["min_naics_share"], pp["min_direction_share"]),
        min_sources,
    )
    phen_cands = [p.candidate for p in phenomena]
    score(phen_cands, rk["weights"], cfg["detect"]["detectors"], rk["max_candidates"])
    members_of = dict(zip(cids, clusters, strict=True))
    context_rows = [
        {
            "run_id": run_id,
            "candidate_id": c.candidate_id,
            "payload_json": json.dumps(
                phenomenon_context(con, cfg, members_of[c.candidate_id], info, emb, refs),
                default=str,
            ),
        }
        for c in candidates
        if c.kind == "series" and c.status == "candidato"
    ]
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
                "rate_effect": rate_by_cid[c.candidate_id].rate_effect
                if c.candidate_id in rate_by_cid
                else None,
                "rate_share": rate_by_cid[c.candidate_id].rate_share
                if c.candidate_id in rate_by_cid
                else None,
                "rate_q": rate_by_cid[c.candidate_id].q_bh
                if c.candidate_id in rate_by_cid
                else None,
                "rate_n_tests": rate_by_cid[c.candidate_id].n_tests
                if c.candidate_id in rate_by_cid
                else None,
                "inflation_effect": infl_by_cid[c.candidate_id].rate_effect
                if c.candidate_id in infl_by_cid
                else None,
                "inflation_share": infl_by_cid[c.candidate_id].rate_share
                if c.candidate_id in infl_by_cid
                else None,
                "inflation_q": infl_by_cid[c.candidate_id].q_bh
                if c.candidate_id in infl_by_cid
                else None,
                "inflation_n_tests": infl_by_cid[c.candidate_id].n_tests
                if c.candidate_id in infl_by_cid
                else None,
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
    by_cid = {c.candidate_id: c for c in candidates}
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
        "n_phenomena": len(phenomena),
        "n_phenomena_multi": sum(len(p.cluster_ids) > 1 for p in phenomena),
        "n_phenomena_candidates": sum(p.candidate.status == "candidato" for p in phenomena),
        "n_phenomena_by_grouping": sum(
            p.candidate.status == "candidato"
            and not any(by_cid[c].status == "candidato" for c in p.cluster_ids)
            for p in phenomena
        ),
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
        "link_funnel": funnel,
        "rate_effect": rate_summary,
        "inflation_effect": infl_summary,
        "naics_coverage": naics_cov,
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
    _write("rank_phenomena", phenomena_rows(run_id, phenomena, candidates), cfg)
    _write("rank_evidence", ev_rows, cfg)
    _write("rank_origin", origin_rows(run_id, candidates, info), cfg)
    _write("rank_context", context_rows, cfg)
    _write("naics_links", naics_mod.rows(run_id, naics_links, tables), cfg)
    _write("rank_runs", [run_row], cfg)
    if log is not None:
        log.info("ranking", extra={"fields": summary})
    return summary


def phenomena_rows(
    run_id: str, phenomena: list[Any], candidates: list[Candidate]
) -> list[dict[str, Any]]:
    by_cid = {c.candidate_id: c for c in candidates}
    rows = []
    for p in phenomena:
        c = p.candidate
        rows.append(
            {
                "run_id": run_id,
                "phenomenon_id": p.phenomenon_id,
                "status": c.status,
                "rank": c.rank,
                "score": c.score,
                "direction": p.direction,
                "naics_json": json.dumps([[n, round(w, 4)] for n, w in p.naics]),
                "window_start": p.window_start,
                "window_end": p.window_end,
                "cluster_ids_json": json.dumps(p.cluster_ids),
                "n_clusters": len(p.cluster_ids),
                "n_members": c.n_members,
                "n_sources": c.n_sources,
                "n_sources_public": c.n_sources_public,
                "sources_json": json.dumps([sorted(g) for g in c.groups]),
                "components_json": json.dumps(c.components) if c.components else None,
                "by_grouping": c.status != "una_fuente"
                and not any(by_cid[x].status == "candidato" for x in p.cluster_ids),
                "published": c.published,
            }
        )
    return rows


def origin_rows(
    run_id: str, candidates: list[Candidate], info: dict[tuple[str, str], SeriesInfo]
) -> list[dict[str, Any]]:
    """Origen, cadena y posibles duplicados de cada serie que es evidencia."""
    rows = {}
    for c in candidates:
        for e in c.evidence:
            si = info.get((e.source, e.object_id))
            if e.kind != "series" or si is None:
                continue
            rows[(e.source, e.object_id)] = {
                "run_id": run_id,
                "source": e.source,
                "object_id": e.object_id,
                "origin_json": json.dumps(sorted(si.agencies)),
                "chain_json": json.dumps(list(si.chain)),
                "rule_id": si.rule_id,
                "possible_same_origin_json": json.dumps(sorted(si.duplicates)),
                "duplicate_rho": si.duplicate_rho,
            }
    return [rows[k] for k in sorted(rows)]


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
    t: dict[str, Any],
    n_entities: int | None,
    cos: float | None,
    linked: str | None,
    *,
    validated: bool | None = None,
    comove: dict[str, Any] | None = None,
) -> Evidence:
    """Evidencia de un término. Con un vínculo a una serie (`cos` no nulo) el término solo es una
    fuente independiente si el co-movimiento lo validó: la similitud de títulos nunca basta, y
    sin validar queda como contexto, con `agencies` vacío (no cuenta en la triangulación)."""
    counts = cos is None or bool(validated)
    return Evidence(
        kind="term",
        source=t["source"],
        object_id=t["object_id"],
        agencies=text_agencies(t["source"]) if counts else frozenset(),
        q_value=float(t["q_value"]),
        effect_size=float(t["effect_size"]),
        detectors=(t["detector"],),
        public=True,
        window_start=t["window_start"],
        window_end=t["window_end"],
        link_score=cos,
        linked_series=linked,
        n_entities=n_entities,
        link_validated=validated if cos is not None else None,
        link_q=comove["q_bh"] if comove else None,
        link_lag=comove["lag"] if comove else None,
    )


def phenomenon_context(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    members: list[tuple[str, str]],
    info: dict[tuple[str, str], SeriesInfo],
    emb: Any,
    refs: set[tuple[str, str]],
) -> dict[str, Any]:
    """Relacionadas y rezagos del candidato (descriptivo; src/rank/context.py)."""
    c = cfg["context"]
    con.register(
        "_core",
        pa.table({"source": [k[0] for k in members], "series_id": [k[1] for k in members]}),
    )
    # Candidatas: misma rama de category_path que algún miembro, sin copyright.
    pool = con.execute(
        """
        WITH b AS (
          SELECT DISTINCT unnest(string_split(m.category_path, ';')) AS branch
          FROM series_meta m JOIN _core USING (source, series_id)
        )
        SELECT DISTINCT m.source, m.series_id, m.title FROM series_meta m,
          (SELECT unnest(string_split(category_path, ';')) AS branch, source, series_id
           FROM series_meta WHERE NOT copyrighted) x
        WHERE x.source = m.source AND x.series_id = m.series_id AND x.branch <> ''
          AND x.branch IN (SELECT branch FROM b)
        ORDER BY 1, 2
        """
    ).fetchall()
    member_set = set(members)
    rows = [
        {"source": s_, "series_id": i, "title": t or i, "downloaded": (s_, i) in info}
        for s_, i, t in pool
        if (s_, i) not in refs and (s_, i) not in member_set
    ]
    # Prefiltro barato para las no descargadas (no tienen embedding en caché): solapamiento de
    # palabras del título con el núcleo; las descargadas usan el embedding cacheado.
    core_titles = [info[k].title for k in members if k in info]
    words = {w for t in core_titles for w in t.casefold().split()}
    down = [r for r in rows if r["downloaded"]]
    miss = sorted(
        (r for r in rows if not r["downloaded"]),
        key=lambda r: (
            -len(words & set(r["title"].casefold().split())),
            r["source"],
            r["series_id"],
        ),
    )[: c["pool_max"]]
    pool_rows = down + miss
    if not pool_rows or not core_titles:
        return {"members": members, "related": [], "n_pool": len(rows)}
    core_vec = emb.embed(core_titles).mean(axis=0)
    core_vec = core_vec / (np.linalg.norm(core_vec) or 1.0)
    vecs = emb.embed([r["title"] for r in pool_rows])
    order = np.argsort(-(vecs @ core_vec), kind="stable")[: 2 * c["pool_max"]]
    pool_rows = [pool_rows[i] for i in sorted(order)]
    vecs = vecs[sorted(order)]
    keys = [(r["source"], r["series_id"]) for r in pool_rows if r["downloaded"]]
    units = (
        dict(
            ((s_, i), u)
            for s_, i, u in con.execute(
                "SELECT source, series_id, any_value(units) FROM series JOIN "
                "(SELECT unnest(?) AS source, unnest(?) AS series_id) k USING (source, series_id) "
                "GROUP BY ALL",
                [[k[0] for k in keys], [k[1] for k in keys]],
            ).fetchall()
        )
        if keys
        else {}
    )
    for r in pool_rows:
        r["units"] = units.get((r["source"], r["series_id"]), "")
    growth = growth_of(con, cfg, sorted(set(keys) | {k for k in members if k in info}))
    related = select_related(members, pool_rows, core_vec, vecs, growth, c)
    return {
        "members": members,
        "related": [asdict(r) for r in related],
        "n_pool": len(rows),
    }


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
