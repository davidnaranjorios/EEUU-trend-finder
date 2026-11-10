"""Demanda real (ADR 0018): `valor` nominal cuenta solo si sobrevive al deflactor; cobertura."""

from __future__ import annotations

import json
from datetime import date

import numpy as np

from src.catalog import connect
from src.config import load_config
from src.rank import inflation_effect as infl_mod
from src.rank import rate_effect as rx
from src.rank.cluster import GrowthSeries
from src.rank.real_effect import real_effect
from src.rank.run import rank_run
from src.rank.triangulate import Candidate, Evidence, assess
from src.rank.watchlist import WatchParams, assess_watch
from src.stats.transform import grid_date
from tests.fakes_rank import build

P = WatchParams(q=0.05, min_effect=1.0)
N = 120
LAST = 2024 * 12 + 11  # índice mensual del último periodo
FIRST = LAST - N + 1
WS, WE = grid_date(LAST - 23, "M"), grid_date(LAST, "M")  # ventana de 24 meses


def _infl(level=0.004, jump=0.0):
    """Inflación mensual: `level`, más `jump` en los últimos 24 meses."""
    pi = {FIRST - 5 + i: level for i in range(N + 5)}
    for i in range(LAST - 23, LAST + 1):
        pi[i] += jump
    return {"M": pi, "Q": {}, "A": {}}


def _growth(infl, *, real_jump=0.0, noise=0.0005, seed=0):
    rng = np.random.default_rng(seed)
    pi = np.array([infl["M"][FIRST + t] for t in range(N)])
    g = pi + 0.001 + rng.normal(0, noise, N)  # crece como los precios
    g[-24:] += real_jump
    return GrowthSeries(g, LAST, "M", scale=0.001, persistence=3)


def ev(kind, *, effect=2.0, real=None, agency="bls", q=1e-6, sub=None):
    return Evidence(
        "series", "bls", "V", frozenset({agency}), q, effect, ("slope",), True, WS, WE,
        measure_kind=kind, measure_subtype=sub, real_effect=real,
    )  # fmt: skip


def watch(evs, params=P):
    cand = assess(Candidate("p1", "phenomenon", evs, len(evs)), 2)
    return assess_watch(cand, {"c1": evs}, {}, params)


# --- efecto real ----------------------------------------------------------------------------


def test_nominal_acceleration_that_is_only_inflation_has_no_real_effect():
    infl = _infl(jump=0.006)  # la inflación se acelera en la ventana y la serie la sigue
    g = _growth(infl, real_jump=0.0)
    r = real_effect(g, infl, WS, WE)
    assert r is not None and abs(r) < 1.0  # deflactada, la aceleración desaparece


def test_real_growth_acceleration_survives_the_deflator():
    infl = _infl(jump=0.006)
    g = _growth(infl, real_jump=0.006)
    r = real_effect(g, infl, WS, WE)
    assert r is not None and r > 3.0  # ≈ 0,006 / escala 0,001


def test_real_effect_is_none_when_it_cannot_be_measured():
    infl = _infl()
    g = _growth(infl)
    assert real_effect(g, {"M": {}, "Q": {}, "A": {}}, WS, WE) is None  # sin deflactor
    early = grid_date(FIRST + 5, "M")  # ventana previa de igual largo no cabe
    assert real_effect(g, infl, early, WE) is None
    short = {"M": {k: v for k, v in infl["M"].items() if k > LAST - 30}, "Q": {}, "A": {}}
    assert real_effect(g, short, WS, WE) is None  # deflactor con huecos en la ventana previa


# --- lista en observación -------------------------------------------------------------------


def test_nominal_value_alone_is_excluded_with_its_own_reason():
    w = watch([ev("valor", real=0.2)])
    assert not w.eligible and w.reason == "solo_valor_nominal"
    w = watch([ev("valor", real=None)])  # sin deflactor no se puede afirmar que no sea inflación
    assert not w.eligible and w.reason == "solo_valor_nominal"


def test_value_with_a_real_effect_enters_and_records_it():
    w = watch([ev("valor", effect=2.0, real=1.6)])
    assert w.eligible and w.real_effect == 1.6 and w.effect == 1.6 and w.n_demand_series == 1


def test_real_effect_must_keep_the_sign_and_reach_the_threshold():
    assert not watch([ev("valor", effect=2.0, real=-2.0)]).eligible
    assert watch([ev("valor", effect=-2.0, real=-1.5)]).eligible
    assert not watch([ev("valor", effect=2.0, real=0.99)]).eligible


def test_quantity_never_needs_a_deflator_and_a_real_value_does_not_rescue_prices():
    assert watch([ev("cantidad", real=None)]).eligible
    w = watch([ev("precio", real=5.0)])
    assert not w.eligible and w.reason == "sin_evidencia_de_demanda"
    w = watch([ev("valor", real=0.1), ev("cantidad")])  # la cantidad sí cuenta
    assert w.eligible and w.real_effect is None


# --- cobertura de los efectos de tasas e inflación -----------------------------------------


def _items(sizes):
    rng = np.random.default_rng(1)
    return {
        f"c{i}": (rng.normal(0, 0.01, n), LAST, grid, 0.01, 3) for i, (grid, n) in enumerate(sizes)
    }


def test_growth_requirement_today_versus_the_slope_requirement():
    p = infl_mod.params_from(json.loads(json.dumps(load_config())))
    assert rx.min_growth_periods(p, "M") == (180, 120)
    assert rx.min_growth_periods(p, "Q") == (73, 53)
    assert rx.min_growth_periods(p, "A") == (47, 40)


def test_breakdown_counts_by_grid_and_kind_and_the_slope_alternative():
    from src.config import load_config

    p = infl_mod.params_from(json.loads(json.dumps(load_config())))
    items = _items([("M", 180), ("M", 130), ("Q", 60), ("A", 15)])
    res = {
        "c0": rx.RateResult("c0", True),
        "c1": rx.RateResult("c1", False, reason="serie_corta"),
        "c2": rx.RateResult("c2", False, reason="serie_corta"),
        "c3": rx.RateResult("c3", False, reason="serie_corta"),
    }
    kinds = {"c0": "precio", "c1": "precio", "c2": "precio", "c3": "valor"}
    out = rx.breakdown(items, res, p, kinds)
    assert out["by_grid"]["M"] == {
        "clusters": 2,
        "evaluated": 1,
        "marked": 0,
        "reasons": {"serie_corta": 1},
    }
    assert out["by_measure_kind"]["precio"]["clusters"] == 3
    assert out["by_measure_kind"]["valor"]["reasons"] == {"serie_corta": 1}
    # Con la exigencia de slope (W + línea base): M 130 >= 120 y Q 60 >= 53 se evaluarían; A 15 no.
    assert out["serie_corta_evaluable_with_slope_requirement"] == {"M": 1, "Q": 1}
    assert out["growth_periods"]["Q"] == {"required_now": 73, "required_by_slope": 53}


# --- integración ---------------------------------------------------------------------------


def test_rank_run_records_real_effect_and_coverage_without_changing_the_ranking(tmp_path):
    fx = build(tmp_path)
    summary = rank_run(fx.cfg, "rank_real")
    for key in ("rate_effect", "inflation_effect"):
        s = summary[key]
        assert s["available"]
        cov = s["coverage"]
        assert set(cov) == {
            "by_grid",
            "by_measure_kind",
            "growth_periods",
            "serie_corta_evaluable_with_slope_requirement",
        }
        assert sum(r["clusters"] for r in cov["by_measure_kind"].values()) == s["n_clusters"]
    con = connect(fx.cfg["paths"]["processed_dir"])
    assert "real_effect" in {r[0] for r in con.execute("DESCRIBE rank_evidence").fetchall()}
    assert "watch_real_effect" in {r[0] for r in con.execute("DESCRIBE rank_phenomena").fetchall()}
    # La regla de origen no cambia: el ranking sigue exigiendo >= 2 fuentes.
    assert (
        con.execute(
            "SELECT count(*) FROM rank_candidates WHERE status = 'candidato' AND n_sources < 2"
        ).fetchone()[0]
        == 0
    )
    # Ninguna serie que no sea `valor` lleva efecto real.
    bad = con.execute(
        "SELECT count(*) FROM rank_evidence WHERE real_effect IS NOT NULL "
        "AND measure_kind IS DISTINCT FROM 'valor'"
    ).fetchone()[0]
    assert bad == 0


def test_date_helper_is_consistent():
    assert grid_date(LAST, "M") == date(2024, 12, 1)


def test_population_counts_are_quantities_but_not_market_demand():
    w = watch([ev("cantidad", sub="poblacion")])
    assert not w.eligible and w.reason == "solo_poblacion"
    # Si además hay una cantidad que no es población, esa sí cuenta (y la población no suma).
    w = watch([ev("cantidad", sub="poblacion"), ev("cantidad")])
    assert w.eligible and w.n_demand_series == 1
    # Un valor nominal tiene prioridad en el motivo: el problema ahí es la inflación.
    w = watch([ev("cantidad", sub="poblacion"), ev("valor", real=0.1)])
    assert not w.eligible and w.reason == "solo_valor_nominal"
