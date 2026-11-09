"""Embudo de enlaces término–serie (auditoría f9189307): alineación de frecuencias y conteos."""

from __future__ import annotations

import numpy as np

from src.catalog import connect
from src.config import load_config
from src.rank.cluster import GrowthSeries
from src.rank.comove import max_overlap
from src.rank.run import comove_validate, rank_run
from tests.fakes_rank import build

YEARS = list(range(2005, 2027))  # 22 años de término


def _term_counts(share: np.ndarray, n_docs: float = 5000.0) -> dict[int, tuple[float, float]]:
    return {
        y: (float(max(1, round(p * n_docs))), n_docs) for y, p in zip(YEARS, share, strict=True)
    }


def _monthly_growth(annual: np.ndarray, noise: float, rng) -> np.ndarray:
    """Crecimiento mensual cuya suma por año es `annual` (+ ruido que no cambia el total)."""
    g = np.repeat(annual / 12, 12)
    e = rng.normal(0, noise, g.size).reshape(-1, 12)
    e -= e.mean(axis=1, keepdims=True)
    return g + e.ravel()


def test_term_comoving_with_monthly_series_links_and_unrelated_pair_does_not():
    rng = np.random.default_rng(11)
    cfg = load_config()
    drive = rng.normal(0, 1, len(YEARS))
    # La proporción del término sigue a `drive` en logit; la serie mensual suma `drive` por año.
    logit = np.cumsum(0.4 * drive)
    share = 1 / (1 + np.exp(-(logit - 6)))
    start = 2005 * 12
    related = GrowthSeries(
        _monthly_growth(0.05 * drive, 0.002, rng), start + 12 * len(YEARS) - 1, "M"
    )
    unrelated = GrowthSeries(
        _monthly_growth(0.05 * rng.normal(0, 1, len(YEARS)), 0.002, rng),
        start + 12 * len(YEARS) - 1,
        "M",
    )
    term = ("edgar", "item_1a|xyz")
    counts = {term: _term_counts(share)}
    growth = {("fred", "REL"): related, ("fred", "UNREL"): unrelated}
    stats: dict[str, int] = {}
    pairs = [(term, ("fred", "REL"))]
    # Relleno nulo para que la familia BH sea realista: 30 series independientes.
    for i in range(30):
        key = ("fred", f"N{i}")
        growth[key] = GrowthSeries(
            _monthly_growth(0.05 * rng.normal(0, 1, len(YEARS)), 0.002, rng),
            start + 12 * len(YEARS) - 1,
            "M",
        )
        pairs.append((term, key))
    pairs.append((term, ("fred", "UNREL")))
    res = comove_validate(cfg, counts, growth, pairs, stats)
    assert res[(term, ("fred", "REL"))]["q_bh"] <= cfg["fdr"]["q"]
    assert res[(term, ("fred", "UNREL"))]["q_bh"] > cfg["fdr"]["q"]
    assert stats["pairs_in"] == len(pairs) and stats["tested"] == len(pairs)
    assert stats["bh"] >= 1 and stats["with_data"] == len(pairs)


def test_pairs_with_too_little_overlap_are_dropped_not_tested():
    cfg = load_config()
    rng = np.random.default_rng(3)
    term = ("edgar", "item_1a|abc")
    counts = {term: _term_counts(rng.uniform(0.01, 0.02, len(YEARS)))}
    short = GrowthSeries(rng.normal(0, 0.01, 24), 2026 * 12 - 1, "M")  # 2 años
    stats: dict[str, int] = {}
    res = comove_validate(cfg, counts, {("fred", "S"): short}, [(term, ("fred", "S"))], stats)
    assert res == {} and stats["with_data"] == 1 and stats["tested"] == 0


def test_max_overlap_counts_common_periods_across_lags():
    a = np.array([1.0, 2.0, np.nan, 4.0, 5.0, 6.0])
    b = np.array([np.nan, 1.0, 2.0, 3.0, 4.0, 5.0])
    assert max_overlap(a, b, 0) == 4 and max_overlap(a, b, 2) >= 4


def test_rank_run_records_link_funnel(tmp_path):
    fx = build(tmp_path)
    summary = rank_run(fx.cfg, "rank_funnel")
    f = summary["link_funnel"]
    for key in (
        "n_terms",
        "pairs_similarity_universe",
        "pairs_similarity_total",
        "pairs_window_overlap",
        "comove_with_data",
        "comove_tested",
        "comove_bh",
        "terms_validated",
    ):
        assert key in f
    assert (
        f["pairs_similarity_total"] >= f["pairs_in_cluster_universe"] >= f["pairs_window_overlap"]
    )
    assert f["comove_tested"] <= f["comove_with_data"] <= f["pairs_window_overlap"]
    con = connect(fx.cfg["paths"]["processed_dir"])
    (params,) = con.execute(
        "SELECT params_json FROM rank_runs WHERE run_id='rank_funnel'"
    ).fetchone()
    assert "link_funnel" in params
