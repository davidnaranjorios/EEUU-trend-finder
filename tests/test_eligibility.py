"""Embudo de elegibilidad: cada regla, el representante de variantes, la geografía y el linaje."""

import copy
from datetime import UTC, date, datetime

import pytest

from src import eligibility as el
from src.config import load_config
from src.ingest.fred import SOURCE, Paths
from src.ingest.store import read_table
from src.schemas import ELIGIBILITY

TODAY = date(2026, 10, 21)
NATIONAL = {"geo_types": ["nation"], "is_usa": True, "release_ids": [1], "agencies": ["A"]}


def row(sid, **kw):
    r = {
        "series_id": sid,
        "title": f"Serie {sid}",
        "frequency_short": "M",
        "units": "Index",
        "seasonal_adjustment": "SA",
        "observation_start": date(1990, 1, 1),
        "observation_end": date(2026, 9, 1),
        "discontinued": False,
        "copyrighted": False,
    }
    r.update(kw)
    return r


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config())


def decide(catalog, cfg, attrs=None):
    attrs = attrs if attrs is not None else {r["series_id"]: NATIONAL for r in catalog}
    return {d["series_id"]: d for d in el.classify(catalog, attrs, cfg, TODAY)}


def test_rules_in_order(cfg):
    catalog = [
        row("OK"),
        row("DISC", discontinued=True, frequency_short="SA"),  # regla 1 gana a la 2
        row("FREQ", frequency_short="BW"),
        row("NODATE", observation_start=None),
        row("STALE", observation_end=date(2025, 1, 1)),
    ]
    d = decide(catalog, cfg)
    assert d["OK"]["group_name"] == "nacional" and d["OK"]["rule"] == "geografia"
    assert d["DISC"]["rule"] == "descontinuada"
    assert d["FREQ"]["rule"] == "frecuencia_no_soportada" and d["FREQ"]["detail"] == "BW"
    assert d["FREQ"]["frequency"] is None
    assert d["NODATE"]["rule"] == "sin_fechas"
    assert d["STALE"]["rule"] == "inactiva" and d["STALE"]["detail"] == "ultimo_dato=2025-01-01"
    assert all(d[s]["group_name"] == "excluida" for s in ("DISC", "FREQ", "NODATE", "STALE"))


def test_staleness_uses_config(cfg):
    catalog = [row("S", observation_end=date(2026, 5, 1))]  # 173 días
    assert decide(catalog, cfg)["S"]["rule"] == "inactiva"  # umbral mensual: 120
    cfg["eligibility"]["max_staleness_days"]["M"] = 200
    assert decide(catalog, cfg)["S"]["rule"] == "geografia"


def test_short_history_goes_to_new_group_not_discarded(cfg):
    catalog = [row("NEW", observation_start=date(2022, 1, 1))]
    d = decide(catalog, cfg)["NEW"]
    assert d["group_name"] == "nuevas" and d["rule"] == "historia_corta"
    assert d["geo_level"] == "nacional"  # conserva su nivel geográfico
    cfg["eligibility"]["min_history_years"] = 3
    assert decide(catalog, cfg)["NEW"]["group_name"] == "nacional"


def test_one_version_per_variable(cfg):
    t = "Ventas totales"
    catalog = [
        row("V_NSA", title=t, seasonal_adjustment="NSA"),
        row("V_SA", title=t),
        row("V_SA_PCH", title=t, units="Percent Change from Year Ago"),
        row("V_SA_Q", title=t, frequency_short="Q"),
        row(
            "V_SA_D_SHORT",
            title=t,
            frequency_short="D",
            observation_start=date(2020, 1, 1),
            observation_end=date(2026, 10, 20),
        ),
        row("OTRA", title="Otra variable"),
    ]
    d = decide(catalog, cfg)
    # SA antes que NSA; nivel antes que transformada; la frecuencia más alta que cumple la
    # historia mínima (la diaria no la cumple) y no la más alta a secas.
    assert d["V_SA"]["group_name"] == "nacional"
    for sid in ("V_NSA", "V_SA_PCH", "V_SA_Q", "V_SA_D_SHORT"):
        assert d[sid]["rule"] == "variante" and d[sid]["detail"] == "variante_de=V_SA"
    assert d["OTRA"]["group_name"] == "nacional"


def test_variants_ignore_case_and_spaces_but_not_geography(cfg):
    catalog = [
        row("A1", title="Empleo en  Texas", seasonal_adjustment="NSA"),
        row("A2", title="empleo en texas"),
        row("B1", title="Empleo en Ohio"),
    ]
    d = decide(catalog, cfg)
    assert d["A1"]["detail"] == "variante_de=A2"
    assert d["B1"]["group_name"] == "nacional"


def test_inactive_variant_does_not_win(cfg):
    catalog = [
        row("OLD_SA", title="X", observation_end=date(2020, 1, 1)),
        row("NEW_NSA", title="X", seasonal_adjustment="NSA"),
    ]
    d = decide(catalog, cfg)
    assert d["OLD_SA"]["rule"] == "inactiva"
    assert d["NEW_NSA"]["group_name"] == "nacional"


def test_variant_preference_from_config(cfg):
    catalog = [row("N", title="X", seasonal_adjustment="NSA"), row("S", title="X")]
    cfg["eligibility"]["variants"]["seasonal_preference"] = ["NSA", "SA", "SAAR", "SSA", "NSAAR"]
    assert decide(catalog, cfg)["S"]["detail"] == "variante_de=N"


@pytest.mark.parametrize(
    ("attrs", "level"),
    [
        ({"geo_types": ["nation"], "is_usa": True}, "nacional"),
        ({"geo_types": ["state"], "is_usa": True}, "regional"),
        ({"geo_types": ["county", "msa"], "is_usa": True}, "regional"),
        ({"geo_types": ["nation"], "is_usa": False}, "internacional"),
        ({"geo_types": ["transnational"], "is_usa": True}, "internacional"),
        ({"geo_types": ["state"], "is_usa": False}, "internacional"),
        ({"geo_types": [], "is_usa": True}, "sin_geografia"),
        (None, "sin_geografia"),
    ],
)
def test_geography(cfg, attrs, level):
    attrs_map = {"S": {"release_ids": [], "agencies": [], **attrs}} if attrs else {}
    d = decide([row("S")], cfg, attrs_map)["S"]
    assert d["geo_level"] == level
    assert d["group_name"] == level  # regional e internacional son grupos, no descartes


def test_copyright_marked_not_excluded(cfg):
    d = decide([row("C", copyrighted=True)], cfg)["C"]
    assert d["copyrighted"] and d["group_name"] == "nacional"


def test_every_series_has_exactly_one_decision(cfg):
    catalog = [
        row(f"S{i}", title=f"T{i % 7}", seasonal_adjustment=("SA", "NSA")[i % 2]) for i in range(50)
    ]
    catalog += [row("D", discontinued=True), row("F", frequency_short="5Y")]
    out = el.classify(catalog, {r["series_id"]: NATIONAL for r in catalog}, cfg, TODAY)
    assert sorted(d["series_id"] for d in out) == sorted(r["series_id"] for r in catalog)
    assert all(d["rule"] for d in out)


def test_funnel_counts_and_sizes(cfg):
    catalog = [
        row("N1"),
        row("N2", frequency_short="Q"),
        row("R1", title="Regional"),
        row("X", discontinued=True),
    ]
    attrs = {
        "N1": NATIONAL,
        "N2": {**NATIONAL, "release_ids": [2]},
        "R1": {"geo_types": ["state"], "is_usa": True, "release_ids": [1], "agencies": ["A"]},
        "X": NATIONAL,
    }
    decisions = el.classify(catalog, attrs, cfg, TODAY)
    f = el.funnel(decisions, catalog, attrs, v2_page_limit=500)
    assert f["total"] == 4
    assert f["excluidas_por_regla"] == {"descontinuada": 1}
    assert f["grupos"] == {"nacional": 2, "regional": 1, "excluida": 1}
    nac = f["tamano_por_grupo"]["nacional"]
    # 1990-01-01..2026-09-01 son 13.392 días: 440 obs mensuales y 147 trimestrales.
    assert nac["series"] == 2 and nac["obs_estimadas"] == 440 + 147
    # Release 1 trae N1, R1 y X (la excluida también viaja en v2): 3 x 440 obs -> 3 páginas.
    assert nac["v2_releases"] == 2 and nac["v2_paginas"] == 3 + 1


def test_lineage_written_and_idempotent(cfg, tmp_path):
    paths = Paths(tmp_path / "processed", tmp_path / "raw")
    catalog = [row("A"), row("B", discontinued=True)]
    decisions = el.classify(catalog, {"A": NATIONAL, "B": NATIONAL}, cfg, TODAY)
    t = datetime(2026, 10, 21, tzinfo=UTC)
    assert el.write_lineage(decisions, "r1", t, paths, n_buckets=4) == 2
    assert el.write_lineage(decisions, "r1", t, paths, n_buckets=4) == 0  # misma corrida
    el.write_lineage(decisions, "r2", t.replace(hour=1), paths, n_buckets=4)
    table = read_table(ELIGIBILITY, paths.eligibility, source=SOURCE)
    assert table.num_rows == 4
    latest = el.latest_decisions(paths)
    assert {r["run_id"] for r in latest} == {"r2"}
    assert {r["series_id"]: r["rule"] for r in latest} == {"A": "geografia", "B": "descontinuada"}
