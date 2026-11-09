"""Anclaje NAICS (ADR 0012): solo tablas oficiales; un test por método; cobertura; esquema."""

from __future__ import annotations

import csv
import json

import pyarrow as pa
import pytest

from src.config import REPO_ROOT, load_config
from src.rank import naics as nx
from src.rank.run import rank_run
from src.schemas import NAICS_LINKS, SchemaError, validate
from tests.fakes_rank import build


@pytest.fixture(scope="module")
def tables():
    return nx.load_tables(load_config(), REPO_ROOT)


def _csv(name):
    with (REPO_ROOT / "config" / "naics" / name).open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_tables_are_official_and_consistent(tables):
    # La concordancia solo apunta a códigos de la tabla oficial; las fuentes están documentadas.
    assert all(r["naics"] in tables.names for r in _csv("sic_naics_concordance.csv"))
    assert (REPO_ROOT / "config" / "naics" / "SOURCES.md").read_text().count("census.gov") >= 1
    for sic, pairs in tables.concordance.items():
        assert len(sic) == 4 and abs(sum(w for _, w in pairs) - 1) < 1e-3


def test_method_code_in_metadata_accepts_only_official_codes(tables):
    ok = nx.series_links({("bls", "a"): "Employment, NAICS 3361 | x"}, tables)
    assert ok[("bls", "a")] == [("3361", 1.0, "codigo_en_metadatos")]
    assert tables.names["3361"]
    bad = nx.series_links(
        {("fred", "b"): "NAICS 9999 and NAICS 99", ("fred", "c"): "sin código 3361"}, tables
    )
    assert bad == {}


def test_method_cluster_inheritance_only_fills_members_without_code(tables):
    own = nx.series_links(
        {("bls", "a"): "NAICS 3361", ("bea", "b"): "NAICS 3362", ("fred", "c"): "nada"}, tables
    )
    out = nx.inherit_in_clusters(
        [[("bls", "a"), ("bls", "d"), ("fred", "c")], [("fred", "z")]],
        {**own, ("bls", "d"): own[("bls", "a")]},
    )
    assert set(out) == {("fred", "c")}  # los miembros con código propio no se tocan
    out = nx.inherit_in_clusters(
        [[("bls", "a"), ("bea", "b"), ("fred", "c")], [("fred", "z")]], own
    )
    assert out == {}  # 50 % / 50 %: ningún código tiene mayoría, no se hereda nada


def test_method_sic_concordance_weights_by_documents(tables):
    sic = next(s for s, p in tables.concordance.items() if len(p) == 1)
    other = next(s for s, p in tables.concordance.items() if len(p) == 1 and s != sic)
    got = nx.term_links({("edgar", "t"): {sic: 3, other: 1, "0000": 5}}, tables)
    w = {n: round(x, 2) for n, x, m in got[("edgar", "t")] if m == "sic_concordancia"}
    assert sum(w.values()) == pytest.approx(1, abs=0.02)
    assert nx.term_links({("edgar", "t"): {}}, tables) == {}  # pendiente de datos: sin filas
    assert nx.parse_sic_top([json.dumps({"2834": 2}), None, json.dumps({"2834": 1})]) == {"2834": 3}


def test_coverage_by_source():
    links = {("bls", "a"): [("31", 1.0, "codigo_en_metadatos")]}
    cov = nx.coverage([("bls", "a"), ("bls", "b"), ("edgar", "t")], links)
    assert cov["bls"] == {"n": 2, "with_naics": 1, "pct": 50.0}
    assert cov["edgar"]["pct"] == 0.0


def test_schema_rejects_unknown_method_and_bad_weight(tables):
    row = {
        "run_id": "r",
        "source": "bls",
        "object_id": "a",
        "naics": "3361",
        "naics_name": "x",
        "peso": 1.0,
        "metodo": "codigo_en_metadatos",
    }
    validate(pa.Table.from_pylist([row], schema=NAICS_LINKS.schema), NAICS_LINKS)
    for bad in ({"metodo": "a_mano"}, {"peso": 0.0}):
        with pytest.raises(SchemaError):
            validate(pa.Table.from_pylist([{**row, **bad}], schema=NAICS_LINKS.schema), NAICS_LINKS)


def test_rank_run_writes_naics_links_and_coverage(tmp_path):
    fx = build(tmp_path)
    summary = rank_run(fx.cfg, "rank1")
    cov = summary["naics_coverage"]
    assert cov and all(0 <= v["pct"] <= 100 for v in cov.values())
    from src.catalog import connect

    con = connect(fx.cfg["paths"]["processed_dir"])
    con.execute("SELECT count(*) FROM naics_links").fetchone()  # la vista existe


# --- S1b: formatos reales por fuente -----------------------------------------------------------

from src.ingest.bls import NAICS_SUFFIX, parse_naics_column  # noqa: E402

# Filas con el formato de ce.industry (BLS): industry_code, naics_code, ..., industry_name
CE_INDUSTRY = [
    ["industry_code", "naics_code", "publishing_status", "industry_name", "display_level"],
    ["00000000", "-", "A", "Total nonfarm", "0"],
    ["20000000", "23", "A", "Construction", "2"],
    ["31000000", "31-33", "A", "Manufacturing", "2"],
    ["42000000", "42", "A", "Wholesale trade", "2"],
]


def test_bls_naics_code_comes_from_the_industry_table(tables):
    m = parse_naics_column(CE_INDUSTRY, "industry_code")
    assert "00000000" not in m and m["31000000"] == "NAICS 31-33"
    links = nx.series_links(
        {("bls", "CES3100000001"): f"All employees | CES;{m['31000000']}"}, tables
    )
    assert links[("bls", "CES3100000001")][0][:3] == ("31-33", 1.0, "codigo_en_metadatos")
    assert NAICS_SUFFIX == "#naics"


REAL_SHAPES = {
    (
        "census",
        "eits/qss:x",
    ): "Quarterly Services Survey | 2017 NAICS code: 5411 | Seasonally adjusted",
    ("bea", "GDPbyIndustry:1:RT"): "Value Added by Industry | Industry: Retail trade",
    ("fred", "USCONS"): "All Employees, Construction",
    ("fred", "XYZ"): "Real Gross Domestic Product",
}


def test_each_source_with_own_codes_has_nonzero_coverage(tables):
    links = nx.series_links(REAL_SHAPES, tables)
    cov = nx.coverage(REAL_SHAPES, links)
    assert links[("census", "eits/qss:x")][0][0] == "5411"
    assert links[("bea", "GDPbyIndustry:1:RT")][0][:3] == ("44-45", 1.0, "nombre_oficial")
    assert links[("fred", "USCONS")][0][0] == "23"
    for src in ("census", "bea"):
        assert cov[src]["pct"] > 0, f"{src}: cobertura 0 % con códigos propios"
    assert cov["fred"] == {"n": 2, "with_naics": 1, "pct": 50.0}


def test_origin_inheritance_and_not_applicable_sources(tables):
    own = nx.series_links({("bls", "A"): "NAICS 3361"}, tables)
    got = nx.inherit_by_origin({("fred", "B"): [("bls", "A")], ("fred", "C"): []}, own)
    assert got[("fred", "B")][0] == ("3361", 1.0, "heredado_origen") and ("fred", "C") not in got
    cov = nx.coverage([("federal_register", "t"), ("bls", "A")], own, ["federal_register"])
    assert set(cov) == {"bls"}


# --- Auditoría f9189307: NAICS más estricto -------------------------------------------------------


@pytest.mark.parametrize(
    ("sid", "code"),
    [
        ("PCU334513334513", "334513"),  # ids reales de PPI de BLS: industria + producto
        ("PCU524126524126", "524126"),
        ("PCU524100524100", "5241"),  # relleno de ceros: 524100 no existe, 5241 sí
    ],
)
def test_naics_embedded_in_bls_ppi_ids(tables, sid, code):
    got = nx.series_links({("bls", sid): "PPI Industry data | not seasonally adjusted"}, tables)
    assert got[("bls", sid)] == [(code, 1.0, "codigo_en_id")]
    # FRED republica los mismos ids PCU; otra fuente con un id parecido no se interpreta.
    assert ("fred", sid) in nx.series_links({("fred", sid): "x"}, tables)
    assert nx.series_links({("census", sid): "x"}, tables) == {}


def test_id_code_must_exist_in_official_table_and_jolts_shape(tables):
    assert nx.id_codes("bls", "PCU999999999999", tables) == []
    assert nx.id_codes("bls", "JTS230000000000000JOL", tables) == ["23"]  # JOLTS construcción
    assert nx.id_codes("bls", "JTS000000000000000JOL", tables) == []  # total: sin industria
    assert nx.id_codes("bls", "CES3100000001", tables) == []  # formato sin NAICS directo


def test_metadata_code_wins_over_id_code(tables):
    got = nx.series_links({("bls", "PCU334513334513"): "x | NAICS 3345"}, tables)
    assert got[("bls", "PCU334513334513")] == [("3345", 1.0, "codigo_en_metadatos")]


def test_name_match_requires_the_full_official_name(tables):
    # "Accommodation" (721) es solo un fragmento de "Accommodation and Food Services" (72).
    got = nx.series_links({("bea", "a"): "Value added | Accommodation and Food Services"}, tables)
    assert [c for c, _, _ in got[("bea", "a")]] == ["72"]
    # Un fragmento suelto de un nombre más largo no ancla nada.
    assert nx.series_links({("bea", "b"): "Employment in Arts, Entertainment"}, tables) == {}
    # El nombre oficial con comas sigue casando completo.
    full = nx.series_links({("bea", "c"): "GDP | Arts, Entertainment, and Recreation"}, tables)
    assert [c for c, _, _ in full[("bea", "c")]] == ["71"]


def test_title_listing_several_sectors_splits_the_weight(tables):
    title = "Arts, Entertainment, and Recreation and Accommodation and Food Services"
    got = nx.series_links({("bea", "a"): title}, tables)[("bea", "a")]
    assert sorted((c, w, m) for c, w, m in got) == [
        ("71", 0.5, "nombre_oficial"),
        ("72", 0.5, "nombre_oficial"),
    ]


def test_cluster_inherits_only_with_weighted_majority(tables):
    own = {
        ("bls", "a"): [("3361", 1.0, "codigo_en_metadatos")],
        ("bls", "b"): [("3361", 1.0, "codigo_en_id")],
        ("bea", "c"): [("5417", 1.0, "nombre_oficial")],
    }
    members = [("bls", "a"), ("bls", "b"), ("bea", "c"), ("fred", "x")]
    out = nx.inherit_in_clusters([members], own, 0.5)
    assert out[("fred", "x")] == [("3361", 1.0, "heredado_cluster")]  # 2/3 > 0.5; 5417 no hereda
    assert nx.inherit_in_clusters([members], own, 0.7) == {}  # umbral configurable
    solo = nx.inherit_in_clusters([[("bea", "c"), ("fred", "x")]], own, 0.5)
    assert solo[("fred", "x")] == [("5417", 1.0, "heredado_cluster")]
