"""Fase 5: identidad de fuente, clusters, cruces, triangulación, score e idempotencia."""

from __future__ import annotations

import json

import numpy as np
import pytest

from src.catalog import connect
from src.rank.agencies import (
    agency_key,
    independent_groups,
    n_independent,
    series_agencies,
    text_agencies,
    unmatched_adapter_agencies,
)
from src.rank.cluster import (
    ClusterParams,
    GrowthSeries,
    cluster_labels,
    coarsen,
    growth_correlation,
    similarity,
)
from src.rank.embed import CachedEmbedder, HashingEmbedder
from src.rank.links import LinkParams, null_threshold, sample_null, top_links
from src.rank.run import rank_run
from src.rank.triangulate import Candidate, Evidence, assess, score
from tests.fakes_rank import build

# --- identidad de fuente ------------------------------------------------------------------------


def test_agency_key_normalizes_fred_names():
    assert agency_key("U.S. Bureau of Labor Statistics") == "bureau of labor statistics"
    assert agency_key("US Census Bureau") == agency_key("U.S. Census Bureau")


def test_same_agency_from_fred_and_adapter_is_one_source():
    fred = series_agencies("fred", ["U.S. Bureau of Labor Statistics"])
    bls = series_agencies("bls")
    assert fred == bls
    assert n_independent([fred, bls]) == 1
    assert n_independent([bls, text_agencies("edgar")]) == 2


def test_shared_agency_chains_into_one_source():
    a = frozenset({"x"})
    ab = frozenset({"x", "y"})
    b = frozenset({"y"})
    assert n_independent([a, ab, b]) == 1  # conservador: x y y se tocan en una evidencia
    assert len(independent_groups([a, b])) == 2


def test_unmatched_adapter_agencies_reports_missing_names():
    assert unmatched_adapter_agencies([]) == []
    assert unmatched_adapter_agencies(["U.S. Bureau of Labor Statistics"]) == ["bea", "census"]


# --- triangulación (propiedad) ------------------------------------------------------------------


def _ev(agencies, public=True, kind="series", q=1e-4):
    return Evidence(kind, "s", f"o{id(agencies)}", frozenset(agencies), q, 1.0, ("slope",), public)


def test_no_candidate_has_a_single_source_property():
    rng = np.random.default_rng(0)
    pool = ["a", "b", "c", "d", "texto:x", "texto:y"]
    cands = []
    for i in range(500):
        ev = []
        for _ in range(int(rng.integers(1, 5))):
            k = int(rng.integers(0, 3))
            ag = set(rng.choice(pool, size=k, replace=False)) if k else set()
            ev.append(_ev(ag, public=bool(rng.random() < 0.7)))
        cands.append(assess(Candidate(f"c{i}", "series", ev, len(ev)), 2))
    score(
        cands,
        dict.fromkeys(("strength", "n_sources", "effect", "agreement", "breadth", "size"), 1.0),
        ["slope", "regime"],
        10_000,
    )
    for c in cands:
        known = [e.agencies for e in c.evidence if e.agencies]
        if c.status == "candidato":
            assert c.n_sources >= 2 and n_independent(known) >= 2
        if c.published:
            assert n_independent([e.agencies for e in c.evidence if e.agencies and e.public]) >= 2
        if n_independent(known) < 2:
            assert c.status == "una_fuente" and not c.published


def test_score_follows_config_weights():
    strong = assess(Candidate("strong", "series", [_ev({"a"}, q=1e-12), _ev({"b"}, q=1e-12)], 2), 2)
    weak = assess(
        Candidate("weak", "series", [_ev({"a"}, q=1e-3), _ev({"b"}, q=1e-3), _ev({"c"})], 9), 2
    )
    zero = dict.fromkeys(("strength", "n_sources", "effect", "agreement", "breadth", "size"), 0.0)
    score([strong, weak], {**zero, "strength": 1.0}, ["slope"], 10)
    assert strong.rank == 1
    score([strong, weak], {**zero, "size": 1.0}, ["slope"], 10)
    assert weak.rank == 1
    score([strong, weak], {**zero, "size": 1.0}, ["slope"], 1)
    assert strong.status == "fuera_del_tope" and not strong.published


# --- clusters ------------------------------------------------------------------------------------


def test_coarsen_sums_complete_quarters_only():
    g = GrowthSeries(np.ones(7), last_index=2026 * 12 + 6, grid="M")  # dic-2025..jun-2026
    q = coarsen(g, "Q")
    assert q == {2026 * 4 + 0: 3.0, 2026 * 4 + 1: 3.0}


def test_growth_correlation_across_grids_and_duplicates():
    rng = np.random.default_rng(1)
    base = rng.standard_normal(180)
    other = rng.standard_normal(180)
    last = 2025 * 12 + 11
    m1 = GrowthSeries(base, last, "M")
    m2 = GrowthSeries(base.copy(), last, "M")
    m3 = GrowthSeries(other, last, "M")
    qa = GrowthSeries(base.reshape(-1, 3).sum(axis=1), 2025 * 4 + 3, "Q")
    overlap = {"M": 36, "Q": 12, "A": 8}
    rho = growth_correlation([m1, m2, m3, qa], overlap)
    assert rho[0, 1] == pytest.approx(1.0)
    assert abs(rho[0, 2]) < 0.3
    assert rho[0, 3] == pytest.approx(1.0)  # mensual agregado a trimestral = el trimestral
    cos = np.array([[1, 1, 0.1, 0.9], [1, 1, 0.1, 0.9], [0.1, 0.1, 1, 0.1], [0.9, 0.9, 0.1, 1]])
    p = ClusterParams(0.5, 0.8, 0.98, 0.9, overlap, 100)
    labels = cluster_labels(similarity(rho, cos, p), p.threshold)
    assert labels[0] == labels[1] == labels[3] != labels[2]


# --- cruces --------------------------------------------------------------------------------------


def test_links_use_data_driven_threshold_and_top_k():
    """Vectores al azar (como embeddings reales: coseno de base no nulo) y un par verdadero."""
    rng = np.random.default_rng(2)

    def unit(m):
        return m / np.linalg.norm(m, axis=1, keepdims=True)

    series = unit(rng.standard_normal((3000, 128)))
    vocab = unit(rng.standard_normal((500, 128)))  # términos comunes sin señal
    terms = unit(np.vstack([series[17] + 0.2 * rng.standard_normal(128), rng.standard_normal(128)]))
    p = LinkParams(null_quantile=0.95, null_terms=500, top_k=1, seed=1)
    thr = null_threshold(vocab, series, p)
    assert 0.2 < thr < 0.6  # la mejor coincidencia por azar de un término cualquiera
    links = top_links(terms, series, thr, p)
    assert [(t, j) for t, j, _ in links] == [(0, 17)]  # el término al azar no se enlaza
    many = LinkParams(null_quantile=0.95, null_terms=500, top_k=3, seed=1)
    assert len(top_links(terms[:1], series, -1.0, many)) == 3  # tope por término
    assert sample_null(["a", "b", "c"], {"b"}, many) == ["a", "c"]


def test_embedding_cache_is_reused(tmp_path):
    emb = CachedEmbedder(HashingEmbedder(), tmp_path)
    a = emb.embed(["uno dos", "tres"])
    again = CachedEmbedder(HashingEmbedder(), tmp_path)
    b = again.embed(["tres", "uno dos"])
    assert again.computed == 0 and again.reused == 2
    np.testing.assert_allclose(a[::-1], b)


# --- corrida completa ----------------------------------------------------------------------------


@pytest.fixture
def ranked(tmp_path):
    fx = build(tmp_path)
    summary = rank_run(fx.cfg, "rank1")
    return fx, summary


def _tables(cfg):
    con = connect(cfg["paths"]["processed_dir"])
    return con


def test_rank_run_triangulates_as_designed(ranked):
    fx, summary = ranked
    con = _tables(fx.cfg)
    cands = {
        r[0]: r
        for r in con.execute(
            "SELECT candidate_id, kind, status, n_sources, n_sources_public, published "
            "FROM rank_candidates WHERE run_id = 'rank1'"
        ).fetchall()
    }
    member = dict(
        ((s, o), c)
        for c, s, o in con.execute(
            "SELECT cluster_id, source, object_id FROM clusters WHERE run_id = 'rank1'"
        ).fetchall()
    )
    dup_f, dup_b = member[fx.ids["dup_fred"]], member[fx.ids["dup_bls"]]
    assert dup_f == dup_b  # el mismo dato de FRED y BLS en un cluster
    assert cands[dup_f][2] == "una_fuente" and cands[dup_f][3] == 1

    linked = cands[member[fx.ids["linked"]]]
    assert linked[2] == "candidato" and linked[3] == 2 and linked[5]

    cr = cands[member[fx.ids["cr"]]]
    assert cr[2] == "candidato" and cr[3] == 2 and cr[4] == 1 and not cr[5]

    assert cands[member[fx.ids["noagency"]]][2] == "una_fuente"

    text = [c for c in cands.values() if c[1] == "text"]
    assert len(text) == 1 and text[0][2] == "candidato" and text[0][5]

    # Ningún candidato con una sola fuente (criterio de terminado).
    assert all(c[3] >= 2 for c in cands.values() if c[2] == "candidato")
    assert all(c[4] >= 2 for c in cands.values() if c[5])
    assert summary["n_published"] == 2
    links = con.execute(
        "SELECT source, series_id, text_source, method FROM series_term_links WHERE run_id='rank1'"
    ).fetchall()
    assert (*fx.ids["linked"], "edgar") in {r[:3] for r in links}
    assert all(r[3].startswith("hashing-") for r in links)


def test_rank_run_is_idempotent(ranked):
    fx, _ = ranked
    con = _tables(fx.cfg)
    tables = ("clusters", "series_term_links", "rank_candidates", "rank_evidence", "rank_runs")
    first = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    rank_run(fx.cfg, "rank1")
    con = _tables(fx.cfg)
    second = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    assert first == second


def test_rank_evidence_records_agencies(ranked):
    fx, _ = ranked
    con = _tables(fx.cfg)
    rows = con.execute(
        "SELECT source, object_id, agencies_json, public FROM rank_evidence WHERE run_id='rank1'"
    ).fetchall()
    by = {(s, o): (json.loads(a), p) for s, o, a, p in rows}
    assert by[fx.ids["dup_fred"]][0] == by[fx.ids["dup_bls"]][0] == ["bureau of labor statistics"]
    assert by[fx.ids["cr"]][1] is False
