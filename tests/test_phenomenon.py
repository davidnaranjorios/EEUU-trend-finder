"""Ficha del fenómeno (descriptiva): relacionadas, rol, transformaciones, rezagos, referencias."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from src.catalog import connect
from src.config import load_config
from src.detect.run import load_series, reference_keys
from src.rank.cluster import GrowthSeries
from src.rank.context import best_lag, role_of, select_related
from src.rank.run import rank_run
from src.report import phenomenon as ph
from src.report.build import build_site
from tests.fakes_rank import build

CTX = load_config()["context"]


def _months(n, start=2000):
    return [date(start + i // 12, i % 12 + 1, 1) for i in range(n)]


def test_role_from_units_only():
    roles = CTX["roles"]
    assert role_of("Percent", roles) == "tasa"
    assert role_of("Thousands of Persons", roles) == "empleo"
    assert role_of("Index 2017=100", roles) == "precio"
    assert role_of("Millions of Dollars", roles) == "dinero"
    assert role_of("Thousands of Units", roles) == "cantidad"
    assert role_of("Widgets", roles) == "otro"


def test_nominal_units_rule():
    assert ph.is_nominal("Millions of Dollars", CTX)
    assert not ph.is_nominal("Millions of Chained 2017 Dollars", CTX)
    assert not ph.is_nominal("Index 1982-1984=100", CTX)
    assert not ph.is_nominal("Percent", CTX)


def test_transformations_on_synthetic_series():
    d = _months(60)
    v = [100 * 1.10 ** (i / 12) for i in range(60)]  # +10 % anual exacto
    assert ph.yoy(d, v)[-1][1] == pytest.approx(10.0)
    assert ph.base100(d, v, d[12])[12] == pytest.approx(100.0)
    defl = ph.monthly_lookup(d, [100 * 1.04 ** (i / 12) for i in range(60)])  # inflación 4 %
    rd, rv = ph.real(d, v, "M", defl)
    assert rv[-1] == pytest.approx(v[-1])  # dólares del último periodo
    assert (rv[-1] / rv[-13] - 1) == pytest.approx(1.10 / 1.04 - 1)
    pop = ph.monthly_lookup(d, [1000.0] * 60)  # "Thousands" de personas
    pd, pv = ph.per_capita(d, v, "Millions of Dollars", "M", pop, "Thousands")
    assert pv[0] == pytest.approx(100 * 1e6 / 1e6)
    seas = [x * (1.05 if dd.month == 12 else 1.0) for dd, x in zip(d, v, strict=True)]
    flat = ph.deseasonalize(d, seas, "M")
    g = [math.log(b / a) for a, b in zip(flat, flat[1:], strict=False)]
    assert max(g) - min(g) < 1e-9  # sin el salto de diciembre
    assert ph.recession_bands(d[:6], [0, 1, 1, 0, 0, 1]) == [(d[1], d[3]), (d[5], d[5])]
    rates = ph.window_rates(d, v, [3])
    assert rates[3][0] == pytest.approx(10.0, abs=0.01)


def test_revision_change_uses_first_covering_vintage():
    d = _months(24)
    early = [(x, 100 + i, date(2002, 2, 1)) for i, x in enumerate(d)]  # publicado en 2002
    late = [(x, 100 + 2 * i, date(2003, 1, 1)) for i, x in enumerate(d)]  # revisado
    cur = (d, [100 + 2 * i for i in range(24)])
    out = ph.revision_change(early + late, cur, (d[0], d[-1]))
    assert out["vintage"] == date(2002, 2, 1) and out["then"][0] < out["now"][0]


def test_best_lag_recovers_injected_lag():
    rng = np.random.default_rng(3)
    base = rng.standard_normal(200)
    core = GrowthSeries(base[6:], 2020 * 12 + 11, "M")
    other = GrowthSeries(base[:-6], 2020 * 12 + 11, "M")  # va 6 meses detrás
    lag, r, grid = best_lag(core, other, 24, 36)
    assert (lag, grid) == (6, "M") and r == pytest.approx(1.0)


def test_related_selection_is_deterministic_and_data_driven():
    rng = np.random.default_rng(4)
    core_g = rng.standard_normal(120)
    growth = {
        ("s", "core"): GrowthSeries(core_g, 2020 * 12, "M"),
        ("s", "twin"): GrowthSeries(core_g + 0.1 * rng.standard_normal(120), 2020 * 12, "M"),
        ("s", "noise"): GrowthSeries(rng.standard_normal(120), 2020 * 12, "M"),
    }
    pool = [
        {"source": "s", "series_id": "twin", "title": "a", "units": "Percent", "downloaded": True},
        {"source": "s", "series_id": "noise", "title": "b", "units": "Index", "downloaded": True},
        {"source": "s", "series_id": "gone", "title": "c", "units": "", "downloaded": False},
    ]
    vecs = np.array([[1.0, 0.0], [1.0, 0.0], [0.6, 0.8]])  # igual título para twin y noise
    core_vec = np.array([1.0, 0.0])
    a = select_related([("s", "core")], pool, core_vec, vecs, growth, CTX)
    perm = [2, 0, 1]
    b = select_related([("s", "core")], [pool[i] for i in perm], core_vec, vecs[perm], growth, CTX)
    assert [(r.series_id, r.score) for r in a] == [(r.series_id, r.score) for r in b]
    assert [r.series_id for r in a] == ["twin", "noise", "gone"]
    assert a[0].role == "tasa" and a[0].lag == 0 and not a[2].downloaded


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    fx = build(tmp_path_factory.mktemp("pheno"))
    rank_run(fx.cfg, "rank1")
    out = build_site(fx.cfg)
    return fx, Path(out["site_dir"])


def test_reference_series_never_enter_detectors_or_ranking(built):
    fx, _ = built
    con = connect(fx.cfg["paths"]["processed_dir"])
    loaded = {(s.source, s.series_id) for s in load_series(con, fx.cfg)}
    refs = reference_keys(fx.cfg)
    assert len(refs) == 3 and not loaded & refs
    members = {(s, o) for s, o in con.execute("SELECT source, object_id FROM clusters").fetchall()}
    evidence = {
        (s, o) for s, o in con.execute("SELECT source, object_id FROM rank_evidence").fetchall()
    }
    assert not (members | evidence) & refs


def test_phenomenon_in_site_without_copyright(built):
    fx, site = built
    index = (site / "index.html").read_text("utf-8")
    assert "Ficha del fenómeno" in index and "base 100" in index
    assert "Zonas grises: recesiones" in index  # indicador de recesión de referencia
    con = connect(fx.cfg["paths"]["processed_dir"])
    payloads = [
        json.loads(p) for (p,) in con.execute("SELECT payload_json FROM rank_context").fetchall()
    ]
    related = [r for p in payloads for r in p["related"]]
    assert related and not any(r["series_id"].startswith("CRF") for r in related)
    for k in range(10):
        assert f"CRF{k}" not in index
    # La serie nominal (dólares) del candidato publicado muestra real y per cápita.
    row = index.split("Real anualizada", 1)[1].split("</tr>", 2)[1]
    assert "Millions of Dollars" in row and "no aplica" not in row and "%" in row


def test_related_selection_is_stable_across_runs(built):
    fx, _ = built
    con = connect(fx.cfg["paths"]["processed_dir"])
    first = con.execute("SELECT payload_json FROM rank_context ORDER BY candidate_id").fetchall()
    rank_run(fx.cfg, "rank2")
    con = connect(fx.cfg["paths"]["processed_dir"])
    second = con.execute(
        "SELECT payload_json FROM rank_context WHERE run_id = 'rank2' ORDER BY candidate_id"
    ).fetchall()
    assert [json.loads(p)["related"] for (p,) in first if p] == [
        json.loads(p)["related"] for (p,) in second
    ]
