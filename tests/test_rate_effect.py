"""Etiqueta "efecto de tasas" (ADR 0013): sintéticos con y sin dependencia de la tasa."""

from __future__ import annotations

import json

import numpy as np
import pytest
import yaml

from src.catalog import connect
from src.config import load_config
from src.rank import rate_effect as rx
from src.rank.run import rank_run
from tests.fakes_rank import build
from tests.guards import hardcoded_topic_violations_yaml

N = 180  # 15 años mensuales
LAST = 2024 * 12 + 11  # índice de grilla del último mes


def _rate_levels(rng, drift=True):
    """Tasa mensual en niveles (puntos porcentuales): camino aleatorio con tendencia."""
    steps = rng.normal(0, 0.05, N + 40)
    if drift:
        steps[-60:] += 0.15  # ciclo de alzas sostenidas durante la ventana reciente
    first = LAST - (N + 40) + 1
    lv = 2 + np.cumsum(steps)
    return {first + i: float(v) for i, v in enumerate(lv)}


def _ar1(rng, n, phi=0.3, sd=0.004):
    e = rng.normal(0, sd, n)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + e[t]
    return x


def _own_trend(rng, n):
    """Crecimiento con aceleración propia en los últimos 5 años, ajena a la tasa."""
    g = 0.002 + _ar1(rng, n)
    g[-60:] += 0.004
    return g


CFG = load_config()


def _params():
    c = json.loads(json.dumps(CFG))
    c["fdr"]["calibration"]["n_resamples"] = 299
    return rx.params_from(c)


def _evaluate(items, levels):
    dr = {g: rx.rate_changes(levels, g) for g in ("M", "Q", "A")}
    return rx.evaluate(
        {k: (g, LAST, "M", float(np.std(g)), 3) for k, g in items.items()}, dr, _params()
    )


def _family(rng, levels, n_null=25):
    dr = rx.rate_changes(levels, "M")
    first = LAST - N + 1
    d = np.array([dr.get(first + t, 0.0) for t in range(N)])
    items = {
        # crecimiento = f(Δtasa) + ruido: toda su aceleración viene de la tasa
        "rate_driven": 0.002 + 0.03 * d + _ar1(rng, N, sd=0.002),
        # aceleración propia, independiente de la tasa
        "own_trend": _own_trend(rng, N),
    }
    for i in range(n_null):  # familia nula realista para la corrección BH
        items[f"null{i}"] = 0.002 + _ar1(rng, N)
    return items


@pytest.fixture(scope="module")
def result():
    rng = np.random.default_rng(5)
    levels = _rate_levels(rng)
    return _evaluate(_family(rng, levels), levels)


def test_series_that_is_a_function_of_the_rate_is_marked(result):
    r = result["rate_driven"]
    assert r.evaluated and r.q_bh <= 0.05 and r.r2 > 0.3
    assert r.rate_effect and r.rate_share >= 0.5


def test_own_trend_independent_of_the_rate_is_not_marked():
    # La tasa no tiene ciclo en la ventana reciente: la aceleración del cluster es propia.
    for seed in range(20, 24):
        rng = np.random.default_rng(seed)
        levels = _rate_levels(rng, drift=False)
        res = _evaluate(_family(rng, levels), levels)
        r = res["own_trend"]
        assert r.evaluated
        assert not r.rate_effect, f"semilla {seed}: marcada sin depender de la tasa"
        assert r.rate_share is None or r.rate_share < 0.5


def test_pure_noise_family_is_rarely_marked_and_records_its_family_size(result):
    nulls = [r for k, r in result.items() if k.startswith("null")]
    assert sum(r.rate_effect for r in nulls) <= 2
    assert all(r.n_tests == len(result) for r in result.values())  # familia de clusters


def test_short_history_or_missing_rate_is_not_evaluated_and_unknown():
    rng = np.random.default_rng(1)
    levels = _rate_levels(rng)
    short = {"s": 0.002 + _ar1(rng, 50)}
    r = _evaluate(short, levels)["s"]
    assert not r.evaluated and not r.rate_effect and r.rate_share is None
    no_rate = rx.evaluate(
        {"s": (_family(rng, levels)["null0"], LAST, "M", 0.004, 3)}, {}, _params()
    )
    assert not no_rate["s"].evaluated and no_rate["s"].rate_share is None


def test_rate_changes_sum_monthly_diffs_per_complete_period():
    lv = {i: float(i * i) for i in range(24)}
    q = rx.rate_changes(lv, "Q")
    assert 0 not in q  # el primer trimestre no tiene el cambio del mes 0: incompleto
    assert q[1] == pytest.approx(5**2 - 2**2)  # meses 3..5: v5 - v2
    assert rx.rate_changes(lv, "A").get(1) == pytest.approx(23**2 - 11**2)


def test_guard_accepts_exactly_the_four_reference_keys_and_rejects_a_fifth(tmp_path):
    base = "context:\n  reference_series:\n    deflator: A\n    population: B\n"
    ok = tmp_path / "ok.yaml"
    ok.write_text(base + "    recessions: C\n    short_rate: D\n", encoding="utf-8")
    assert hardcoded_topic_violations_yaml(ok) == []
    for name, extra in (
        ("five", "    recessions: C\n    short_rate: D\n    other: E\n"),
        ("three", "    recessions: C\n"),
    ):
        bad = tmp_path / f"{name}.yaml"
        bad.write_text(base + extra, encoding="utf-8")
        assert hardcoded_topic_violations_yaml(bad), name
    assert yaml.safe_load(open("config.yaml"))["context"]["reference_series"]["short_rate"]


def test_config_rejects_a_missing_or_extra_reference_key():
    from src.config import ConfigError, validate_config

    cfg = json.loads(json.dumps(CFG))
    cfg["context"]["reference_series"]["other"] = "X"
    with pytest.raises(ConfigError):
        validate_config(cfg)
    cfg = json.loads(json.dumps(CFG))
    del cfg["context"]["reference_series"]["short_rate"]
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_rank_run_records_label_without_filtering_and_never_ranks_the_rate(tmp_path):
    fx = build(tmp_path)
    summary = rank_run(fx.cfg, "rank_rate")
    rs = summary["rate_effect"]
    assert rs["available"] and rs["reference"] == "TB3MS" and rs["n_evaluated"] >= 0
    con = connect(fx.cfg["paths"]["processed_dir"])
    cols = {r[0] for r in con.execute("DESCRIBE rank_candidates").fetchall()}
    assert {"rate_effect", "rate_share", "rate_q", "rate_n_tests"} <= cols
    assert {"rate_effect", "rate_share"} <= {
        r[0] for r in con.execute("DESCRIBE clusters").fetchall()
    }
    # La etiqueta no filtra: todos los clusters siguen en la tabla, marcados o no.
    n_cl = con.execute("SELECT count(DISTINCT cluster_id) FROM clusters").fetchone()[0]
    n_c = con.execute("SELECT count(*) FROM rank_candidates WHERE kind = 'series'").fetchone()[0]
    assert n_cl >= n_c > 0
    ids = {r[0] for r in con.execute("SELECT object_id FROM clusters").fetchall()}
    assert "TB3MS" not in ids  # la tasa es referencia: nunca es miembro de un cluster
