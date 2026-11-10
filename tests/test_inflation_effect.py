"""Etiqueta "efecto inflación" (ADR 0015): sintéticos con y sin dependencia de la inflación."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.catalog import connect
from src.config import ConfigError, load_config, validate_config
from src.rank import inflation_effect as ix
from src.rank.run import rank_run
from tests.fakes_rank import build

N = 180  # 15 años mensuales
LAST = 2024 * 12 + 11
CFG = load_config()


def _cpi(rng, accelerate=True):
    """Índice de precios mensual: inflación AR(1) que se acelera en los últimos 5 años."""
    n = N + 40
    pi = np.zeros(n)
    e = rng.normal(0, 0.0007, n)
    for t in range(1, n):
        pi[t] = 0.0015 + 0.8 * (pi[t - 1] - 0.0015) + e[t]
    if accelerate:
        pi[-60:] += np.linspace(0, 0.004, 60)
    first = LAST - n + 1
    lv = 100 * np.exp(np.cumsum(pi))
    return {first + i: float(v) for i, v in enumerate(lv)}


def _ar1(rng, n, phi=0.3, sd=0.004):
    e = rng.normal(0, sd, n)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + e[t]
    return x


def _params():
    c = json.loads(json.dumps(CFG))
    c["fdr"]["calibration"]["n_resamples"] = 299
    return ix.params_from(c)


def _evaluate(items, levels):
    return ix.evaluate(
        {k: (g, LAST, "M", float(np.std(g)), 3) for k, g in items.items()}, levels, _params()
    )


def _pi(levels):
    d = ix.inflation_by_grid(levels, "M")
    first = LAST - N + 1
    return np.array([d.get(first + t, 0.0) for t in range(N)])


def _family(rng, levels, n_null=25):
    pi = _pi(levels)
    items = {
        # serie nominal = inflación + ruido: toda su aceleración es la de los precios
        "inflation_driven": 0.001 + 1.0 * pi + _ar1(rng, N, sd=0.001),
        # aceleración propia, independiente de la inflación
        "own_trend": 0.002 + _ar1(rng, N),
    }
    items["own_trend"][-60:] += 0.004
    for i in range(n_null):
        items[f"null{i}"] = 0.002 + _ar1(rng, N)
    return items


@pytest.fixture(scope="module")
def result():
    rng = np.random.default_rng(11)
    levels = _cpi(rng)
    return _evaluate(_family(rng, levels), levels)


def test_series_that_is_inflation_plus_noise_is_marked(result):
    r = result["inflation_driven"]
    assert r.evaluated and r.q_bh <= 0.05 and r.q_diff <= 0.05 and r.r2 > 0.3
    assert r.rate_effect and r.rate_share >= 0.5


def test_own_trend_independent_of_inflation_is_not_marked():
    for seed in range(30, 34):
        rng = np.random.default_rng(seed)
        levels = _cpi(rng, accelerate=False)
        r = _evaluate(_family(rng, levels), levels)["own_trend"]
        assert r.evaluated
        assert not r.rate_effect, f"semilla {seed}: marcada sin depender de la inflación"


def test_false_discoveries_stay_controlled_on_noise():
    marked = tested = 0
    for seed in range(40, 44):
        rng = np.random.default_rng(seed)
        levels = _cpi(rng)
        items = {f"n{i}": 0.002 + _ar1(rng, N) for i in range(30)}
        res = _evaluate(items, levels)
        marked += sum(r.rate_effect for r in res.values())
        tested += len(res)
    assert tested == 120
    assert marked / tested <= 0.05  # q = 0.05; en ruido puro casi nunca se marca


def test_family_records_its_size(result):
    assert {r.n_tests for r in result.values() if r.evaluated} == {27}


def test_not_evaluated_clusters_say_why():
    rng = np.random.default_rng(1)
    g = 0.002 + _ar1(rng, N)
    res = ix.evaluate({"a": (g, LAST, "M", 0.004, 3)}, {}, _params())
    assert not res["a"].evaluated and res["a"].reason == "sin_deflactor"
    assert res["a"].rate_effect is False and res["a"].rate_share is None
    short = ix.evaluate({"s": (g[:20], LAST, "M", 0.004, 3)}, _cpi(rng), _params())
    assert not short["s"].evaluated and short["s"].reason == "serie_corta"


def test_inflation_is_the_sum_of_monthly_log_changes_over_complete_periods_only():
    lv = {
        m: 100 * 1.01**m for m in range(0, 7)
    }  # meses 0..6: el trimestre 2 (6..8) está incompleto
    q = ix.inflation_by_grid(lv, "Q")
    assert set(q) == {1}  # el trimestre 0 pierde su primer mes (sin dato previo)
    lv2 = {m: 100 * 1.01**m for m in range(-1, 6)}
    q2 = ix.inflation_by_grid(lv2, "Q")
    assert set(q2) == {0, 1} and q2[0] == pytest.approx(3 * np.log(1.01))
    assert ix.inflation_by_grid(lv2, "M")[2] == pytest.approx(np.log(1.01))


def test_config_validates_the_inflation_options():
    cfg = json.loads(json.dumps(CFG))
    validate_config(cfg)
    cfg["rank"]["inflation_effect"]["min_drop"] = 0
    with pytest.raises(ConfigError):
        validate_config(cfg)
    cfg = json.loads(json.dumps(CFG))
    del cfg["rank"]["inflation_effect"]["max_lag"]
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_no_new_reference_series_key_is_needed():
    # El deflactor ya aprobado es la única fuente: la etiqueta no agrega ids a config.yaml.
    assert set(CFG["context"]["reference_series"]) == {
        "deflator",
        "population",
        "recessions",
        "short_rate",
    }
    assert "series_id" not in json.dumps(CFG["rank"]["inflation_effect"])


def test_rank_run_records_label_without_filtering_and_never_ranks_the_deflator(tmp_path):
    fx = build(tmp_path)
    summary = rank_run(fx.cfg, "rank_infl")
    s = summary["inflation_effect"]
    assert s["reference"] == "CPIAUCSL"
    assert s["available"]  # el fixture trae el deflactor
    assert s["n_evaluated"] + sum(s["not_evaluated_by_reason"].values()) == s["n_clusters"]
    con = connect(fx.cfg["paths"]["processed_dir"])
    cols = {r[0] for r in con.execute("DESCRIBE rank_candidates").fetchall()}
    assert {"inflation_effect", "inflation_share", "inflation_q", "inflation_n_tests"} <= cols
    assert {"inflation_effect", "inflation_share"} <= {
        r[0] for r in con.execute("DESCRIBE clusters").fetchall()
    }
    n_cl = con.execute("SELECT count(DISTINCT cluster_id) FROM clusters").fetchone()[0]
    n_c = con.execute("SELECT count(*) FROM rank_candidates WHERE kind = 'series'").fetchone()[0]
    assert n_cl >= n_c > 0
    ids = {r[0] for r in con.execute("SELECT object_id FROM clusters").fetchall()}
    assert "CPIAUCSL" not in ids


def test_report_shows_the_label_but_never_filters_or_changes_the_score(tmp_path):
    from src.report.build import build_site
    from src.report.render import _inflation_badge

    assert _inflation_badge({"inflation_effect": None}) == ""
    assert _inflation_badge({"inflation_effect": False}) == ""
    badge = _inflation_badge({"inflation_effect": True, "inflation_share": 0.7})
    assert "Efecto inflación" in badge and "70%" in badge
    fx = build(tmp_path)
    rank_run(fx.cfg, "rank_infl2")
    con = connect(fx.cfg["paths"]["processed_dir"])
    before = con.execute(
        "SELECT count(*) FROM rank_candidates WHERE run_id='rank_infl2'"
    ).fetchone()
    con.close()
    site = build_site(fx.cfg)
    assert "Efecto inflación" in (Path(site["site_dir"]) / "index.html").read_text("utf-8")
    assert before[0] > 0
