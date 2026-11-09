"""Enlaces texto–serie trimestrales para el Federal Register (grilla del término configurable)."""

from __future__ import annotations

import json

import duckdb
import numpy as np
import pyarrow as pa
import pytest

from src.config import ConfigError, load_config, validate_config
from src.rank.cluster import GrowthSeries
from src.rank.comove import (
    ComoveParams,
    coarser,
    comove_joint,
    series_grid_growth,
    term_annual_change,
)
from src.rank.run import comove_validate, term_grid_counts, text_grids

FR = "federal_register"
Q0 = 2000 * 4  # primer trimestre: índice año·4 + trimestre
NQ = 100  # 25 años de trimestres
FR_TERM = (FR, "rule|sample term")


def _cfg(text_grid=None):
    cfg = json.loads(json.dumps(load_config()))
    if text_grid is not None:
        cfg["rank"]["links"]["comove"]["text_grid"] = text_grid
    return cfg


def _counts_q(share: np.ndarray, n_docs: float = 4000.0) -> dict[int, tuple[float, float]]:
    return {Q0 + i: (float(max(1, round(p * n_docs))), n_docs) for i, p in enumerate(share)}


def _counts_a(q: dict[int, tuple[float, float]]) -> dict[int, tuple[float, float]]:
    """Los mismos documentos agregados a años (sin interpolar): suma de sus 4 trimestres."""
    acc: dict[int, list[tuple[float, float]]] = {}
    for idx, cn in q.items():
        acc.setdefault(idx // 4, []).append(cn)
    return {y: (sum(c for c, _ in v), sum(n for _, n in v)) for y, v in acc.items() if len(v) == 4}


def _monthly(quarterly: np.ndarray, rng, noise=0.002) -> GrowthSeries:
    """Serie mensual cuya suma por trimestre es `quarterly` (el ruido no cambia el total)."""
    g = np.repeat(quarterly / 3, 3)
    e = rng.normal(0, noise, g.size).reshape(-1, 3)
    e -= e.mean(axis=1, keepdims=True)
    return GrowthSeries(g + e.ravel(), Q0 * 3 + 3 * len(quarterly) - 1, "M")


def _world(rng, relation=1.0, n_null=40):
    """Término trimestral y series: una ligada a él (`relation`), otra libre y relleno nulo."""
    ar = np.zeros(NQ)
    for t in range(1, NQ):
        ar[t] = 0.3 * ar[t - 1] + rng.normal()
    share = 1 / (1 + np.exp(-(np.cumsum(0.25 * ar) - 6)))
    d_logit = np.diff(np.log(share / (1 - share)), prepend=np.nan)
    driver = np.nan_to_num(d_logit)
    growth = {
        ("fred", "REL"): _monthly(relation * 0.04 * driver + 0.01 * rng.normal(size=NQ), rng),
        ("fred", "UNREL"): _monthly(0.04 * rng.normal(size=NQ), rng),
    }
    for i in range(n_null):
        growth[("fred", f"N{i}")] = _monthly(0.04 * rng.normal(size=NQ), rng)
    return _counts_q(share), growth


def test_grid_helpers_never_refine_a_coarser_series():
    assert coarser("Q", "M") == "Q" and coarser("Q", "A") == "A" and coarser("A", "Q") == "A"
    g = np.arange(24, dtype=float)
    q = series_grid_growth(g, Q0 * 3 + 23, "M", "Q")  # 24 meses -> 8 trimestres completos
    assert len(q) == 8 and q[Q0] == pytest.approx(0 + 1 + 2)
    assert series_grid_growth(np.ones(8), Q0 + 7, "Q", "Q") == {Q0 + i: 1.0 for i in range(8)}
    with pytest.raises(ValueError):
        series_grid_growth(np.ones(4), 2003, "A", "Q")  # una serie anual no se interpola
    # Un primer trimestre incompleto (arranca en el mes 2) no se usa.
    assert len(series_grid_growth(np.ones(8), Q0 * 3 + 8, "M", "Q")) == 2


def test_term_grid_counts_aggregate_months_without_interpolating():
    con = duckdb.connect()
    rows = []
    for y in (2020, 2021):
        for m in range(1, 13):
            if (y, m) == (2021, 12):  # trimestre 2021Q4 incompleto: 2 de 3 meses
                continue
            for term, c in (("alpha", 7), ("beta", 5)):
                rows.append(("federal_register", "rule", f"{y}-{m:02d}", term, c, 100))
    for q in range(1, 5):  # EDGAR: trimestres, y un periodo anual más grueso que la grilla
        rows.append(("edgar", "1A", f"2020Q{q}", "alpha", 9, 50))
    rows.append(("edgar", "1A", "2019", "alpha", 9, 50))
    con.register(
        "tf",
        pa.table(
            {
                k: [r[i] for r in rows]
                for i, k in enumerate(
                    ("source", "section", "period", "term", "doc_count", "total_docs")
                )
            }
        ),
    )
    con.execute("CREATE VIEW term_freq AS SELECT * FROM tf")
    sigs = [
        {"source": FR, "object_id": "rule|alpha"},
        {"source": "edgar", "object_id": "1A|alpha"},
    ]
    out = term_grid_counts(con, sigs, min_doc_count=3, grids={"A", "Q"})
    fr_q = out["Q"][(FR, "rule|alpha")]
    assert fr_q[2020 * 4] == (21.0, 300.0)  # 3 meses de 7 documentos y 100 en total
    assert Q0 + 8 * 0 + (2021 - 2000) * 4 + 3 not in fr_q  # 2021Q4 incompleto: fuera
    assert len(fr_q) == 7
    assert out["A"][(FR, "rule|alpha")] == {2020: (84.0, 1200.0)}  # 2021 incompleto: fuera
    # EDGAR en trimestral usa sus trimestres; el periodo anual '2019' no se reparte.
    assert out["Q"][("edgar", "1A|alpha")] == {2020 * 4 + q: (9.0, 50.0) for q in range(4)}
    assert out["A"][("edgar", "1A|alpha")][2020] == (36.0, 200.0)


def test_text_grid_is_configurable_with_federal_register_quarterly_by_default():
    cfg = load_config()
    assert cfg["rank"]["links"]["comove"]["text_grid"] == {FR: "Q", "edgar": "A"}
    assert cfg["rank"]["links"]["comove"]["min_overlap_by_grid"]["Q"] == 24
    assert text_grids(cfg, {FR, "edgar", "otra"}) == {FR: "Q", "edgar": "A", "otra": "A"}
    bad = json.loads(json.dumps(cfg))
    bad["rank"]["links"]["comove"]["text_grid"] = {FR: "M"}
    with pytest.raises(ConfigError):
        validate_config(bad)
    bad = json.loads(json.dumps(cfg))
    del bad["rank"]["links"]["comove"]["min_overlap_by_grid"]["Q"]
    with pytest.raises(ConfigError):
        validate_config(bad)


@pytest.fixture(scope="module")
def related_world():
    rng = np.random.default_rng(21)
    counts_q, growth = _world(rng)
    return counts_q, growth


def _validate(counts_q, growth, text_grid):
    pairs = [(FR_TERM, k) for k in growth]
    counts = {"Q": {FR_TERM: counts_q}, "A": {FR_TERM: _counts_a(counts_q)}}
    stats: dict[str, int] = {}
    return comove_validate(_cfg(text_grid), counts, growth, pairs, stats), stats


def test_quarterly_term_comoving_with_monthly_series_is_linked_and_unrelated_is_not(related_world):
    counts_q, growth = related_world
    res, stats = _validate(counts_q, growth, {FR: "Q"})
    q = _cfg()["fdr"]["q"]
    rel, unrel = res[(FR_TERM, ("fred", "REL"))], res[(FR_TERM, ("fred", "UNREL"))]
    assert rel["grid"] == "Q" and rel["q_bh"] <= q and rel["lag"] == 0
    assert unrel["q_bh"] > q
    assert stats["tested_grid_Q"] == len(growth) and stats["bh"] >= 1
    assert all(r["n_tests"] == len(growth) for r in res.values())  # su propia familia BH


def test_quarterly_has_more_power_than_annual_on_the_same_data(related_world):
    counts_q, growth = related_world
    quarterly, _ = _validate(counts_q, growth, {FR: "Q"})
    annual, _ = _validate(counts_q, growth, {FR: "A"})
    key = (FR_TERM, ("fred", "REL"))
    assert annual[key]["grid"] == "A" and quarterly[key]["grid"] == "Q"
    assert quarterly[key]["p"] < annual[key]["p"]  # ~100 puntos frente a ~25
    assert quarterly[key]["q_bh"] < annual[key]["q_bh"]
    assert quarterly[key]["stat"] > 0 and annual[key]["stat"] > 0


def test_moderate_relation_is_detected_quarterly_but_mostly_missed_annually():
    # Medido (12 semillas, relación 0,5): trimestral detecta 9 de 12, anual 2 de 12; mediana de p
    # 0,001 frente a 0,13. Las cotas del test dejan margen sobre esa medición.
    rng = np.random.default_rng(8)
    det_q = det_a = 0
    p_q, p_a = [], []
    key = (FR_TERM, ("fred", "REL"))
    for _ in range(12):
        counts_q, growth = _world(rng, relation=0.5, n_null=20)
        q_res, _ = _validate(counts_q, growth, {FR: "Q"})
        a_res, _ = _validate(counts_q, growth, {FR: "A"})
        det_q += q_res[key]["q_bh"] <= 0.05
        det_a += a_res[key]["q_bh"] <= 0.05
        p_q.append(q_res[key]["p"])
        p_a.append(a_res[key]["p"])
    assert det_q >= 7 and det_a <= det_q - 4
    assert np.median(p_q) < np.median(p_a) / 10  # el p trimestral es mucho menor


def test_false_discovery_rate_stays_controlled_on_pure_noise_quarterly():
    rng = np.random.default_rng(99)
    q = _cfg()["fdr"]["q"]
    discoveries = tests = families_with_false = 0
    for _ in range(8):
        counts_q, growth = _world(rng, relation=0.0, n_null=40)  # nada está ligado
        res, _ = _validate(counts_q, growth, {FR: "Q"})
        n_sig = sum(r["q_bh"] <= q for r in res.values())
        discoveries += n_sig
        tests += len(res)
        families_with_false += n_sig > 0
    assert discoveries / tests <= 2 * q  # con q = 0,05, muy por debajo en la práctica
    assert families_with_false <= 2  # familias con algún descubrimiento (todos falsos)


def test_annual_series_with_quarterly_term_is_tested_annually_and_edgar_stays_annual():
    rng = np.random.default_rng(4)
    counts_q, growth = _world(rng, n_null=10)
    # Una serie anual nativa: 24 años de crecimiento que terminan en 2024.
    annual = GrowthSeries(rng.normal(0, 0.02, 24), 2024, "A")
    growth[("bea", "ANN")] = annual
    edgar = ("edgar", "1A|x")
    pairs = [(FR_TERM, k) for k in growth] + [(edgar, ("fred", "REL"))]
    counts = {
        "Q": {FR_TERM: counts_q},
        "A": {FR_TERM: _counts_a(counts_q), edgar: _counts_a(counts_q)},
    }
    stats: dict[str, int] = {}
    res = comove_validate(_cfg(), counts, growth, pairs, stats)
    assert res[(FR_TERM, ("bea", "ANN"))]["grid"] == "A"  # serie anual: nunca se interpola
    assert res[(FR_TERM, ("fred", "REL"))]["grid"] == "Q"
    assert res[(edgar, ("fred", "REL"))]["grid"] == "A"  # EDGAR sigue anual
    assert stats["tested_grid_A"] >= 2 and stats["tested_grid_Q"] >= 10
    # Una sola familia BH sobre los dos ejes.
    assert {r["n_tests"] for r in res.values()} == {stats["tested"]}


def test_comove_joint_matches_single_family_for_one_axis():
    rng = np.random.default_rng(2)
    params = ComoveParams(2, 8, 199, "auto", 5)
    pairs = [(rng.normal(size=30), rng.normal(size=30)) for _ in range(5)]
    from src.rank.comove import comove_family

    one = comove_family(pairs, params)
    (joint,) = comove_joint([(pairs, params, [8] * 5)])
    assert [r["n_tests"] for r in joint] == [5] * 5
    assert [round(r["stat"], 9) for r in one] == [round(r["stat"], 9) for r in joint]


def test_term_change_works_on_quarter_indices():
    ch = term_annual_change({Q0: (10, 1000), Q0 + 1: (20, 1000), Q0 + 3: (30, 1000)})
    assert set(ch) == {Q0 + 1}  # solo periodos consecutivos: sin Q0+2, no hay cambio hasta Q0+3
