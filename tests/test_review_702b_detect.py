"""Revisión 702b8ff3: deduplicación previa, resolución de p con m grande."""

from __future__ import annotations

import numpy as np

from src.detect.dedup import dedup_series
from src.detect.run import Series, summarize
from src.stats.fdr import TailConfig, bh_adjust, bootstrap_p


def _s(source, sid, vals, first=0, grid="Q"):
    return Series(source, sid, grid, grid, None, False, np.array(vals, float), first)


def test_dedup_same_bea_code_across_tables_keeps_one_representative():
    v = np.cumsum(np.arange(1, 30.0))
    a = _s("bea", "NIPA:T50306:Y033RG:Q", v)
    b = _s("bea", "NIPA:T50305:Y033RG:Q", v[2:], first=2)  # más corta, mismos valores
    other = _s("bea", "NIPA:T50306:A191RC:Q", v * 2)
    reps, groups = dedup_series([a, b, other])
    assert {r.series_id for r in reps} == {"NIPA:T50306:Y033RG:Q", "NIPA:T50306:A191RC:Q"}
    assert groups == {("bea", "NIPA:T50306:Y033RG:Q"): [("bea", "NIPA:T50305:Y033RG:Q")]}


def test_dedup_same_id_different_values_not_merged_and_fred_vs_original():
    v = np.linspace(1, 50, 40)
    reps, groups = dedup_series([_s("bls", "LNS14000000", v), _s("fred", "LNS14000000", v)])
    assert [r.source for r in reps] == ["bls"] and groups
    reps, groups = dedup_series(
        [_s("bea", "N:T1:Y033RG:Q", v), _s("bea", "N:T2:Y033RG:Q", v[::-1].copy())]
    )
    assert len(reps) == 2 and not groups


def test_bh_recovers_strong_signals_at_m_250k_via_tail():
    rng = np.random.default_rng(0)
    m, n_true = 250_000, 50
    p = rng.uniform(size=m)
    tail = TailConfig()
    for i in range(n_true):
        null = rng.exponential(size=999)
        p[i], method = bootstrap_p(float(null.max()) + 40.0, null, tail)
        assert method == "gpd"
    q = bh_adjust(p)
    assert (q[:n_true] <= 0.05).all()


def test_summarize_reports_when_p_floor_blocks_bh():
    rows = [
        {
            "frequency": "M",
            "source": "fred",
            "q_value": 1.0,
            "q_by": 1.0,
            "p_method": "empirical",
            "copyrighted": False,
            "effect_size": 1.0,
            "p_value": 1e-5,
        }
        for _ in range(262_000)
    ]
    s = summarize(rows, 0.05)
    assert s["p_floor_blocks_bh"] and s["n_bh"] == 0
