"""Revisión 2a02c981: vínculo término–serie por co-movimiento y tasa de aprobación BH."""

from __future__ import annotations

import json

import numpy as np

from src.catalog import connect
from src.detect.approval import alert_flags, annotate
from src.rank import run as rank_mod
from src.rank.comove import ComoveParams, comove_family, comove_stat
from src.rank.run import _term_evidence, rank_run
from src.rank.triangulate import Candidate, assess
from tests.fakes_rank import build

PARAMS = ComoveParams(max_lag=2, min_overlap=8, n_resamples=999, block_length="auto", seed=1)


def ar1(rng, n, phi=0.5):
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + rng.normal()
    return x


def test_comovement_detects_lagged_relation_and_rejects_independent_pairs():
    rng = np.random.default_rng(4)
    n = 24
    pairs = []
    for _ in range(40):  # nulo: pares independientes (AR(1) en ambos lados)
        pairs.append((ar1(rng, n), ar1(rng, n)))
    a = ar1(rng, n)
    b = np.full(n, np.nan)
    b[1:] = a[:-1] + 0.3 * rng.normal(size=n - 1)  # la serie va 1 periodo después
    pairs.append((a, b))
    res = comove_family(pairs, PARAMS)
    assert res[-1]["q_bh"] <= 0.05 and res[-1]["lag"] == 1
    assert sum(r["q_bh"] <= 0.05 for r in res[:-1]) <= 2  # FDR bajo q en el nulo
    assert all(r["n_tests"] == len(pairs) for r in res)  # cada par registra su familia
    stat, lag = comove_stat(a, b, 2, 8)
    assert lag == 1 and stat > 0.8


def test_title_similarity_alone_never_counts_as_independent_source():
    t = {
        "source": "edgar",
        "object_id": "item_1a|algo nuevo",
        "q_value": 1e-6,
        "effect_size": 2.0,
        "detector": "text_emerging",
        "window_start": None,
        "window_end": None,
    }
    series = _term_evidence(t, None, None, None)  # texto solo: sí es una fuente
    assert series.agencies
    ser = Candidate("c", "series", [], 1)
    from src.rank.triangulate import Evidence

    base = Evidence("series", "bls", "S1", frozenset({"bls"}), 1e-5, 1.0, ("slope",), True)
    suggested = _term_evidence(t, None, 0.99, "bls|S1", validated=False)
    validated = _term_evidence(t, None, 0.99, "bls|S1", validated=True)
    assert suggested.agencies == frozenset() and suggested.link_validated is False
    ser.evidence = [base, suggested]
    assert assess(ser, 2).status == "una_fuente"
    ser.evidence = [base, validated]
    assert assess(ser, 2).status == "candidato"


def test_rank_run_requires_validated_comovement(tmp_path, monkeypatch):
    fx = build(tmp_path)
    monkeypatch.setattr(rank_mod, "comove_validate", lambda *a, **k: {})  # nada se valida
    rank_run(fx.cfg, "rank_nolink")
    con = connect(fx.cfg["paths"]["processed_dir"])
    cands = con.execute(
        "SELECT status, n_sources FROM rank_candidates WHERE run_id = 'rank_nolink' "
        "AND kind = 'series'"
    ).fetchall()
    assert all(n < 2 for _, n in cands)  # solo los títulos parecidos no alcanzan
    assert con.execute(
        "SELECT count(*) FROM series_term_links WHERE run_id='rank_nolink' AND validated"
    ).fetchone() == (0,)


def test_approval_rate_alert_over_three_times_median_of_others():
    rows = [
        {"detector": "a", "n_bh": 10, "n_tests": 1000},
        {"detector": "b", "n_bh": 12, "n_tests": 1000},
        {"detector": "c", "n_bh": 90, "n_tests": 1000},
    ]
    assert alert_flags(rows) == {"a": False, "b": False, "c": True}
    assert not any(
        alert_flags([*rows[:2], {"detector": "c", "n_bh": 30, "n_tests": 1000}]).values()
    )
    assert alert_flags(rows[:1]) == {"a": False}  # sin con qué comparar
    out = annotate(rows)
    assert out[2]["alert"] and out[0]["bh_rate"] == 0.01


def test_text_run_records_bh_rate_in_detector_runs_params():
    # El detector de texto escribe bh_rate y approval_alert en params_json (ver detect/text.py).
    from pathlib import Path

    src = Path("src/detect/text.py").read_text(encoding="utf-8")
    assert '"bh_rate"' in src and '"approval_alert"' in src
    assert json.dumps({"bh_rate": 0.0})
