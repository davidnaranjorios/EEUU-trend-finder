"""Lista «en observación» (ADR 0016): un origen + demanda entra; el ranking no cambia."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.catalog import connect
from src.config import ConfigError, load_config, validate_config
from src.rank.phenomena import PhenomenaParams, group_phenomena
from src.rank.run import rank_run
from src.rank.triangulate import Candidate, Evidence, assess
from src.rank.watchlist import WatchParams, assess_watch
from src.schemas import RANK_PHENOMENA, SchemaError, validate
from tests.fakes_rank import build

P = WatchParams(q=0.05, min_effect=1.0)
W = (date(2020, 1, 1), date(2024, 1, 1))


def ev(source, sid, agency, kind, *, q=1e-6, effect=2.0, public=True, real=None):
    return Evidence(
        "series", source, sid, frozenset({agency}), q, effect, ("slope",), public, *W,
        measure_kind=kind, real_effect=real,
    )  # fmt: skip


def phenomenon(evidence, min_sources=2):
    return assess(Candidate("p1", "phenomenon", evidence, len(evidence)), min_sources)


def watch(evidence, marked=None, params=P):
    cand = phenomenon(evidence)
    return assess_watch(cand, {"c1": evidence}, marked or {}, params)


def test_one_origin_with_quantity_enters_the_watch_list_and_not_the_ranking():
    c = phenomenon([ev("bls", "A", "bls", "cantidad")])
    w = assess_watch(c, {"c1": c.evidence}, {}, P)
    assert w.eligible and w.reason is None and w.effect == 2.0 and w.n_demand_series == 1
    assert c.status == "una_fuente" and c.rank is None and c.score is None and not c.published


def test_value_series_also_counts_as_demand_evidence():
    # `valor` (dólares corrientes) solo cuenta con efecto real deflactado (ADR 0018).
    assert watch([ev("bea", "V", "bea", "valor", real=2.0)]).eligible


@pytest.mark.parametrize("kind", ["precio", "tasa", "otro", None])
def test_only_prices_rates_or_unknown_stay_out(kind):
    w = watch([ev("bls", "A", "bls", kind)])
    assert not w.eligible and w.reason == "sin_evidencia_de_demanda"


def test_two_origins_go_to_the_ranking_not_the_watch_list():
    evidence = [ev("bls", "A", "bls", "cantidad"), ev("census", "B", "census", "cantidad")]
    c = phenomenon(evidence)
    assert c.status == "candidato"
    w = assess_watch(c, {"c1": evidence}, {}, P)
    assert not w.eligible and w.reason == "triangulado"


def test_rate_or_inflation_effect_excludes_the_quantity_series():
    evidence = [ev("bea", "V", "bea", "valor", real=2.0)]
    w = watch(evidence, marked={"c1": True})
    assert not w.eligible and w.reason == "efecto_tasas_o_inflacion"
    # no evaluado (None) o sin marca (False) no excluye
    assert watch(evidence, marked={"c1": None}).eligible
    assert watch(evidence, marked={"c1": False}).eligible


def test_weak_effect_or_unsignificant_or_copyrighted_stay_out():
    assert watch([ev("bls", "A", "bls", "cantidad", effect=0.4)]).reason == "efecto_bajo"
    assert watch([ev("bls", "A", "bls", "cantidad", q=0.2)]).reason == "sin_senal"
    w = watch([ev("fred", "X", "fred", "cantidad", public=False)])
    assert not w.eligible and w.reason == "sin_evidencia_publica"


def test_demand_must_sit_in_an_unmarked_cluster_of_the_phenomenon():
    a, b = ev("bls", "A", "bls", "cantidad"), ev("bls", "B", "bls", "cantidad")
    cand = phenomenon([a, b])
    out = assess_watch(cand, {"c1": [a], "c2": [b]}, {"c1": True, "c2": False}, P)
    assert out.eligible and out.n_demand_series == 1


def test_group_phenomena_keeps_a_single_origin_cluster_as_a_watch_candidate():
    e = ev("bls", "A", "bls", "cantidad")
    c = assess(Candidate("c1", "series", [e], 1), 2)
    (p,) = group_phenomena(
        [c], {"c1": [("bls", "A")]}, {("bls", "A"): [("5415", 1.0, "x")]},
        PhenomenaParams(3, 0.25, 0.6), 2,
    )  # fmt: skip
    assert p.candidate.status == "una_fuente"
    assert assess_watch(p.candidate, {"c1": c.evidence}, {}, P).eligible


def test_schema_rejects_a_watch_row_that_is_triangulated_published_or_ranked():
    base = {
        "run_id": "r", "phenomenon_id": "p", "status": "una_fuente", "rank": None, "score": None,
        "direction": "sube", "naics_json": "[]", "window_start": W[0], "window_end": W[1],
        "cluster_ids_json": "[]", "n_clusters": 1, "n_members": 1, "n_sources": 1,
        "n_sources_public": 1, "sources_json": "[]", "components_json": None,
        "by_grouping": False, "published": False, "measure_mix_json": None,
        "no_demand_evidence": None, "watch": True, "watch_reason": None, "watch_effect": 2.0,
        "watch_q": 1e-6, "watch_n_demand_series": 1,
    }  # fmt: skip
    import pyarrow as pa

    def check(**over):
        validate(pa.Table.from_pylist([{**base, **over}], schema=RANK_PHENOMENA.schema),
                 RANK_PHENOMENA)  # fmt: skip

    check()
    triangulated = {"status": "candidato", "n_sources": 2, "n_sources_public": 2}
    cases = [
        {"n_sources": 2, "n_sources_public": 2},
        {"rank": 1},
        {"score": 0.5},
        {**triangulated, "published": True},
    ]
    for bad in cases:
        with pytest.raises(SchemaError):
            check(**bad)


def test_config_validates_min_effect():
    cfg = json.loads(json.dumps(load_config()))
    validate_config(cfg)
    cfg["rank"]["single_origin"]["min_effect"] = 0
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_rank_run_records_the_watch_list_without_touching_the_ranking(tmp_path):
    fx = build(tmp_path)
    summary = rank_run(fx.cfg, "rank_watch")
    wl = summary["watchlist"]
    n_ph = summary["n_phenomena"]
    assert wl["n_in"] + sum(wl["excluded_by_reason"].values()) == n_ph
    con = connect(fx.cfg["paths"]["processed_dir"])
    rows = con.execute(
        "SELECT status, n_sources, published, rank, score, watch FROM rank_phenomena "
        "WHERE run_id = 'rank_watch'"
    ).fetchall()
    assert len(rows) == n_ph
    for status, n_sources, published, rank, score, w in rows:
        if w:
            assert status == "una_fuente" and n_sources == 1
            assert not published and rank is None and score is None
    # El ranking de candidatos sigue exigiendo >= 2 fuentes.
    assert (
        con.execute(
            "SELECT count(*) FROM rank_candidates WHERE status = 'candidato' AND n_sources < 2"
        ).fetchone()[0]
        == 0
    )


def test_report_has_a_separate_watch_section_with_the_warning(tmp_path):
    from src.report.render import _watch_card

    card = _watch_card(
        {
            "phenomenon_id": "p1", "effect": 2.5, "q": 1e-5, "n_clusters": 1, "n_members": 2,
            "direction": "sube", "window": W, "n_demand_series": 1, "sources": [["bls"]],
            "naics": [], "measure_mix": {"cantidad": 1}, "clusters": ["c1"],
        }
    )  # fmt: skip
    assert "En observación" in card and "Un solo origen: no triangulado" in card
    assert "🟡" in card

    from src.report.build import build_site

    fx = build(tmp_path)
    rank_run(fx.cfg, "rank_watch2")
    site = build_site(fx.cfg)
    html = (Path(site["site_dir"]) / "index.html").read_text("utf-8")
    assert "🟡 En observación" in html
    assert "Sin fenómenos en observación esta semana" in html or "Un solo origen" in html
