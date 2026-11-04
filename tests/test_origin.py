"""ADR 0010: independencia por ORIGEN del dato (principio 4), no por la agencia que publica."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from src.ingest.store import read_table
from src.rank.agencies import agency_key
from src.rank.origin import (
    DuplicateParams,
    find_duplicates,
    format_chain,
    load_rules,
    merge_identities,
    resolve_origin,
    validate_table,
)
from src.rank.run import rank_run
from src.rank.triangulate import Candidate, Evidence, assess
from src.report.build import build_site
from src.schemas import RANK_ORIGIN
from tests.fakes_rank import build

FED = frozenset({agency_key("Board of Governors of the Federal Reserve System")})
FDIC = frozenset({agency_key("Federal Deposit Insurance Corporation")})
BLS = frozenset({agency_key("U.S. Bureau of Labor Statistics")})
OECD = frozenset({agency_key("Organisation for Economic Co-operation and Development")})
RULES = load_rules()


def _ev(origin, i=0):
    return Evidence("series", "fred", f"S{i}", origin.keys, 1e-6, 1.0, ("slope",), True)


def _sources(*origins) -> int:
    c = Candidate("c", "series", [_ev(o, i) for i, o in enumerate(origins)], len(origins))
    return assess(c, 2).n_sources


def _z1(sid="BOGZ1FL763065105Q"):
    return resolve_origin(RULES, source="fred", series_id=sid, agencies=FED, release_ids=[52])


def _fdic(sid="FDICQ1"):
    return resolve_origin(RULES, source="fred", series_id=sid, agencies=FDIC, release_ids=[482])


def _oecd_wage():
    return resolve_origin(
        RULES, source="fred", series_id="LCEAMN01USM189S", agencies=OECD, release_ids=[205]
    )


def _bls_wage():
    return resolve_origin(RULES, source="bls", series_id="CES0500000003", agencies=BLS)


@pytest.mark.parametrize(
    "a,b",
    [
        pytest.param(_z1("BOGZ1FL763065105Q"), _fdic("FDICHELOC"), id="HELOC_Z1_vs_FDIC"),
        pytest.param(_z1("BOGZ1FL765065105Q"), _fdic("FDICOREO"), id="OREO_Z1_vs_FDIC"),
        pytest.param(_bls_wage(), _oecd_wage(), id="salarios_BLS_vs_OCDE"),
    ],
)
def test_republished_data_counts_as_one_source(a, b):
    assert a.keys == b.keys
    assert _sources(a, b) == 1


def _z1_real(sid, title, units="Millions of U.S. Dollars"):
    return resolve_origin(
        RULES,
        source="fred",
        series_id=sid,
        agencies=FED,
        release_ids=[52],
        units=units,
        title=title,
    )


BEA = frozenset({agency_key("U.S. Bureau of Economic Analysis")})


def test_real_z1_heloc_depository_institutions_matches_fdic():
    """Corrida 702b8ff3: Z.1 sector 70 (Private Depository Institutions) quedaba como Fed."""
    z1 = _z1_real(
        "BOGZ1FL703065125Q", "Private Depository Institutions; Home Equity Loans; Asset, Level"
    )
    assert z1.keys == _fdic("FDICHELOC").keys == frozenset({"call reports"})
    assert _sources(z1, _fdic("FDICHELOC")) == 1


def test_real_z1_price_index_matches_bea():
    """Corrida 702b8ff3: índice de precios de la Z.1 (unidades 'Percent') frente a BEA."""
    z1 = _z1_real(
        "BOGZ1FL075013203Q",
        "Interest Rates and Price Indexes; Expenditures on Equipment; Chain Weights, Level",
        units="Percent",
    )
    bea = resolve_origin(RULES, source="bea", series_id="T20304", agencies=BEA)
    assert z1.keys == bea.keys
    assert _sources(z1, bea) == 1
    assert format_chain(z1.chain) == "BEA → republicado por Fed Z.1"


@pytest.mark.parametrize(
    "sid,title",
    [
        ("BOGZ1FL073161113Q", "Interest Rates and Price Indexes; 10-Year Treasury Yield, Level"),
        ("BOGZ1FL072052006Q", "Interest Rates and Price Indexes; Effective Federal Funds Rate"),
        (
            "BOGZ1FL075035503Q",
            "Interest Rates and Price Indexes; Commercial Real Estate Price Index",
        ),
    ],
)
def test_real_z1_rates_are_not_bea(sid, title):
    assert _z1_real(sid, title, units="Percent").keys == FED


def test_unrelated_agencies_stay_independent():
    other = resolve_origin(
        RULES, source="fred", series_id="X", agencies=frozenset({"census bureau"}), release_ids=[3]
    )
    assert _sources(_z1(), other) == 2
    assert _sources(_bls_wage(), other) == 2


def test_chain_is_readable():
    assert format_chain(_oecd_wage().chain) == "BLS → republicado por OCDE MEI"
    assert format_chain(_fdic().chain) == "Call Reports (FFIEC) → republicado por FDIC QBP"
    assert _fdic().chain[0] == _z1().chain[0]


def test_default_origin_is_the_agency():
    o = resolve_origin(RULES, source="fred", series_id="X", agencies=FED, release_ids=[999])
    assert o.keys == FED and o.chain == () and o.rule_id is None


def test_shipped_table_is_valid_and_loadable():
    data = yaml.safe_load(Path("config/data_origin.yaml").read_text("utf-8"))
    assert len(validate_table(data)) == len(data["rules"]) >= 4


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d.update(extra=1),
        lambda d: d["rules"][0].pop("chain"),
        lambda d: d["rules"][0]["match"].update(topic="x"),
        lambda d: d["rules"][0].update(chain=["solo uno"]),
        lambda d: d["rules"][0]["match"].update(series_id_regex="("),
        lambda d: d["rules"][0]["match"].update(release_id="52"),
        lambda d: d["rules"].append(dict(d["rules"][0])),  # id repetido
        lambda d: d.update(rules=[]),
    ],
)
def test_table_schema_rejects_invalid(mutate):
    data = yaml.safe_load(Path("config/data_origin.yaml").read_text("utf-8"))
    mutate(data)
    with pytest.raises(ValueError):
        validate_table(data)


def test_data_origin_is_not_allowed_inside_config_yaml(tmp_path):
    """La excepción es solo el archivo config/data_origin.yaml; en config.yaml sigue prohibido."""
    from tests.guards import hardcoded_topic_violations_yaml

    p = tmp_path / "config.yaml"
    p.write_text(
        "rank:\n  data_origin:\n    rules:\n      - match: {series_id_regex: BOGZ1FL763065105Q}\n"
        "        origin: call_reports\n",
        encoding="utf-8",
    )
    assert hardcoded_topic_violations_yaml(p)


def _growth(rng, n=120):
    return 0.002 + 0.01 * rng.standard_normal(n)


def test_duplicate_detector_flags_distinct_agencies_with_same_data():
    rng = np.random.default_rng(1)
    g = _growth(rng)
    a = {i: 100 * float(v) for i, v in enumerate(np.exp(np.cumsum(g)))}
    b = {i: v * 1.001 for i, v in a.items()}
    c = {i: v * 3.0 for i, v in a.items()}  # misma forma, otro nivel (otras unidades)
    keys = [("fred", "A"), ("fred", "B"), ("fred", "C")]
    ident = {k: frozenset({n}) for k, n in zip(keys, ("one", "two", "three"), strict=True)}
    levels = {keys[0]: ("M", a), keys[1]: ("M", b), keys[2]: ("M", c)}
    rho = np.ones((3, 3))
    p = DuplicateParams(0.98, 0.02, 12)
    pairs = find_duplicates(keys, rho, levels, ident, p)
    assert [(x, y) for x, y, _ in pairs] == [(keys[0], keys[1])]
    merge_identities(ident, pairs)
    assert ident[keys[0]] == ident[keys[1]] == frozenset({"one", "two"})
    assert ident[keys[2]] == frozenset({"three"})
    # Mismo origen declarado: no hace falta marcar. ρ baja: tampoco.
    assert find_duplicates(keys, rho * 0.9, levels, ident, p) == []


@pytest.fixture(scope="module")
def ranked(tmp_path_factory):
    fx = build(tmp_path_factory.mktemp("origin"))
    rank_run(fx.cfg, "rank1")
    return fx


def _origin_rows(fx):
    from src.rank.run import PARTITION, rank_root

    return {
        (r["source"], r["object_id"]): r
        for r in read_table(
            RANK_ORIGIN, rank_root(fx.cfg, "rank_origin"), source=PARTITION
        ).to_pylist()
    }


def test_rank_run_flags_possible_same_origin_and_merges_sources(ranked):
    rows = _origin_rows(ranked)
    a, b = ranked.ids["mirror_a"], ranked.ids["mirror_b"]
    assert json.loads(rows[a]["possible_same_origin_json"]) == [f"{b[0]}|{b[1]}"]
    assert json.loads(rows[b]["possible_same_origin_json"]) == [f"{a[0]}|{a[1]}"]
    assert rows[a]["duplicate_rho"] > 0.98
    assert json.loads(rows[a]["origin_json"]) == json.loads(rows[b]["origin_json"])
    # Sin duplicado ni regla, nada que marcar.
    assert json.loads(rows[ranked.ids["linked"]]["possible_same_origin_json"]) == []


def test_rank_run_uses_data_origin_table(ranked):
    rows = _origin_rows(ranked)
    z1, fdic = rows[ranked.ids["z1_bank"]], rows[ranked.ids["fdic_bank"]]
    assert z1["origin_json"] == fdic["origin_json"] == json.dumps(["call reports"])
    assert "Fed Z.1" in json.loads(z1["chain_json"])


def test_report_shows_origin_chain_and_flag():
    from src.report.render import _origin_rows

    html = _origin_rows(
        {
            "origin_chain": format_chain(_oecd_wage().chain),
            "possible_same_origin": ["fred|OTHER"],
        }
    )
    assert "BLS → republicado por OCDE MEI" in html
    assert "Posible mismo origen" in html and "fred|OTHER" in html
    assert _origin_rows({}) == ""


def test_published_report_reads_origin_table(ranked):
    out = build_site(ranked.cfg)
    assert out["rank_run"] == "rank1"
