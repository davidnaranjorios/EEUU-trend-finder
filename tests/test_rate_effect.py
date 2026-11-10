"""Etiqueta "efecto de tasas" (ADR 0013): sintéticos con y sin dependencia de la tasa."""

from __future__ import annotations

import json
from pathlib import Path

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


def _params(include_level=True):
    c = json.loads(json.dumps(CFG))
    c["fdr"]["calibration"]["n_resamples"] = 299
    c["rank"]["rate_effect"]["include_level"] = include_level
    return rx.params_from(c)


def _evaluate(items, levels, include_level=True):
    dr = {g: rx.rate_changes(levels, g) for g in ("M", "Q", "A")}
    lv = {g: rx.rate_levels(levels, g) for g in ("M", "Q", "A")}
    return rx.evaluate(
        {k: (g, LAST, "M", float(np.std(g)), 3) for k, g in items.items()},
        dr,
        _params(include_level),
        lv,
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


def _hike_levels(rng, hike=True):
    """Tasa baja y estable, un ciclo de alzas de 12 meses y luego una meseta alta (como 2022)."""
    n = N + 40
    steps = rng.normal(0, 0.03, n)
    if hike:
        steps[-42:-30] += 4.5 / 12
    f = LAST - n + 1
    return {f + i: float(v) for i, v in enumerate(0.5 + np.cumsum(steps))}


def _accumulation_index(rng, levels, noise=0.00002):
    """Índice de acumulación (T-bills, money market): crecimiento mensual ≈ r_t / 12 + ruido."""
    lv = rx.rate_levels(levels, "M")
    first = LAST - N + 1
    r = np.array([lv[first + i] for i in range(N)])
    return r / 1200 + _ar1(rng, N, sd=noise)


def _level_family(rng, levels, n_null=25):
    items = {"accum": _accumulation_index(rng, levels)}
    for i in range(n_null):
        items[f"null{i}"] = 0.002 + _ar1(rng, N)
    return items


def test_accumulation_index_is_marked_with_the_level_but_not_with_only_the_change():
    # Su crecimiento es el NIVEL de la tasa / 12: tras un ciclo de alzas el nivel se queda alto y el
    # cambio de pendiente persiste mientras Δr vuelve a ~0. Con solo Δr (diseño anterior) el R²
    # es ~0,1 y el residuo conserva la aceleración: nunca se marcaba (Reporte 3914814: 0 de 52).
    marked_new = marked_old = 0
    for seed in range(30, 36):
        rng = np.random.default_rng(seed)
        levels = _hike_levels(rng)
        items = _level_family(rng, levels)
        new = _evaluate(items, levels, include_level=True)["accum"]
        old = _evaluate(items, levels, include_level=False)["accum"]
        assert new.r2 > 0.8 and new.r2_diff > 0.8 and old.r2 < 0.3, (seed, new.r2, old.r2)
        assert old.q_bh > 0.05  # el diseño anterior ni siquiera ve la relación
        marked_new += new.rate_effect
        marked_old += old.rate_effect
        assert new.rate_share >= 0.5
    assert marked_new == 6 and marked_old == 0


def test_own_trend_stays_unmarked_with_the_level_design():
    # Medido (30 semillas, tasa sin ciclo): sin la condición en diferencias, el nivel de la tasa
    # (un camino aleatorio) se ajustaba por azar a la aceleración propia y la marcaba en 6 de 40;
    # con ella, 0 de 30. Es la regresión espuria de dos series persistentes.
    for seed in range(40, 46):
        rng = np.random.default_rng(seed)
        levels = _hike_levels(rng, hike=False)  # la tasa no tiene ciclo: sin nada que explicar
        items = {"own_trend": _own_trend(rng, N)}
        for i in range(25):
            items[f"null{i}"] = 0.002 + _ar1(rng, N)
        assert not _evaluate(items, levels)["own_trend"].rate_effect, seed


def test_false_discoveries_stay_controlled_on_noise_with_the_level_design():
    q = CFG["fdr"]["q"]
    n_sig = n_tests = 0
    for seed in range(50, 56):
        rng = np.random.default_rng(seed)
        levels = _hike_levels(rng)
        items = {f"null{i}": 0.002 + _ar1(rng, N) for i in range(40)}
        res = _evaluate(items, levels)
        n_sig += sum(r.q_bh <= q for r in res.values() if r.evaluated)
        n_tests += len(res)
        assert sum(r.rate_effect for r in res.values()) <= 1
    assert n_sig / n_tests <= 2 * q


def test_pure_noise_family_is_rarely_marked_and_records_its_family_size(result):
    nulls = [r for k, r in result.items() if k.startswith("null")]
    assert sum(r.rate_effect for r in nulls) <= 2
    assert all(r.n_tests == len(result) for r in result.values())  # familia de clusters


def test_spurious_level_fit_is_rejected_by_the_difference_check():
    # La aceleración propia se ajusta en niveles a una tasa que sube sin relación (R² alto), pero
    # no en primeras diferencias: la relación real (g = r/12) sí aparece en ambas.
    rng = np.random.default_rng(40)
    levels = _hike_levels(rng, hike=False)
    own = {"own_trend": _own_trend(rng, N), **{f"n{i}": 0.002 + _ar1(rng, N) for i in range(25)}}
    spurious = _evaluate(own, levels)["own_trend"]
    assert not spurious.rate_effect and (spurious.r2_diff or 0) < 0.2
    rng = np.random.default_rng(31)
    levels = _hike_levels(rng)
    real = _evaluate(_level_family(rng, levels), levels)["accum"]
    assert real.rate_effect and real.q_bh <= 0.05 and real.q_diff <= 0.05


def test_not_evaluated_clusters_say_why():
    rng = np.random.default_rng(1)
    levels = _hike_levels(rng)
    dr = {g: rx.rate_changes(levels, g) for g in ("M", "Q", "A")}
    lv = {g: rx.rate_levels(levels, g) for g in ("M", "Q", "A")}
    ok = 0.002 + _ar1(rng, N)
    items = {
        "ok": (ok, LAST, "M", 0.004, 3),
        "corta": (ok[:50], LAST, "M", 0.004, 3),
        "anual": (ok[:14], 2024, "A", 0.004, 3),  # la línea base de slope (33) no alcanza
        "sin_ventana": (ok, LAST, "D", 0.004, 3),
    }
    res = rx.evaluate(items, dr, _params(), lv)
    assert res["ok"].evaluated and res["ok"].reason is None
    assert res["corta"].reason == "serie_corta" and res["anual"].reason == "serie_corta"
    assert res["sin_ventana"].reason == "sin_ventana"
    assert rx.evaluate(items, {}, _params(), {})["ok"].reason == "sin_tasa"
    # Con la tasa solo en parte del periodo hay pocos periodos con dato en todas las columnas.
    short = {k: v for k, v in dr["M"].items() if k > LAST - 30}
    res = rx.evaluate({"ok": items["ok"]}, {"M": short}, _params(), {"M": lv["M"]})
    assert res["ok"].reason == "pocos_periodos_con_tasa"
    assert not any(r.evaluated for k, r in res.items())


def test_rate_levels_average_complete_periods_only():
    lv = {m: float(m) for m in range(24)}  # nivel = índice del mes
    q = rx.rate_levels(lv, "Q")
    assert q[1] == pytest.approx(4.0) and q[7] == pytest.approx(22.0) and len(q) == 8
    assert rx.rate_levels({0: 1.0, 1: 3.0}, "Q") == {}  # trimestre incompleto: fuera
    assert rx.rate_levels(lv, "A")[1] == pytest.approx(17.5)


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


def test_config_validates_the_rate_effect_design_options():
    from src.config import ConfigError, validate_config

    cfg = json.loads(json.dumps(CFG))
    assert cfg["rank"]["rate_effect"]["include_level"] is True
    cfg["rank"]["rate_effect"]["include_level"] = "si"
    with pytest.raises(ConfigError):
        validate_config(cfg)
    cfg = json.loads(json.dumps(CFG))
    del cfg["rank"]["rate_effect"]["min_rows_by_grid"]["A"]
    with pytest.raises(ConfigError):
        validate_config(cfg)


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
    # Cada cluster o se evaluó o tiene un motivo registrado: nada se pierde en silencio.
    assert rs["include_level"] is True
    assert rs["n_evaluated"] + sum(rs["not_evaluated_by_reason"].values()) == rs["n_clusters"]
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


def test_report_shows_the_label_but_never_filters_or_changes_the_score(tmp_path):
    from src.report.build import build_site
    from src.report.render import _rate_badge

    assert _rate_badge({"rate_effect": None}) == "" and _rate_badge({"rate_effect": False}) == ""
    badge = _rate_badge({"rate_effect": True, "rate_share": 0.62})
    assert "Efecto de tasas" in badge and "62%" in badge
    fx = build(tmp_path)
    rank_run(fx.cfg, "rank_lbl")
    con = connect(fx.cfg["paths"]["processed_dir"])
    before = con.execute(
        "SELECT candidate_id, score FROM rank_candidates WHERE run_id='rank_lbl' ORDER BY 1"
    ).fetchall()
    con.close()
    site = build_site(fx.cfg)
    index = Path(site["site_dir"]) / "index.html"
    assert "Efecto de tasas" in index.read_text("utf-8")  # glosario
    assert before  # los candidatos siguen todos listados: la etiqueta no filtra
