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


# --- BEA y Census con código propio (ids reales) -------------------------------------------------

from src.ingest.bea import parse_data  # noqa: E402

# Filas recortadas de respuestas reales de la API de BEA (2026-11-09): GDPbyIndustry (tabla 1),
# UnderlyingGDPbyIndustry (tabla 210) e InputOutput (tabla 57).
BEA_GDPI = {
    "BEAAPI": {
        "Results": [
            {
                "Statistic": "GDP by Industry",
                "Data": [
                    {
                        "TableID": "1",
                        "Frequency": "A",
                        "Year": "2022",
                        "Quarter": "2022",
                        "Industry": ind,
                        "IndustrYDescription": desc,
                        "DataValue": "1.0",
                        "NoteRef": "1",
                    }
                    for ind, desc in (
                        ("111CA", "Farms"),
                        ("44RT", "Retail trade"),
                        ("5415", "Computer systems design and related services"),
                        ("PVT", "Private industries"),
                        ("GDP", "Gross domestic product"),
                    )
                ],
            }
        ]
    }
}
BEA_IO = {
    "BEAAPI": {
        "Results": [
            {
                "Statistic": "Input-Output",
                "Data": [
                    {
                        "TableID": "57",
                        "Year": "2022",
                        "RowCode": "",
                        "RowDescr": "Total industry output requirement",
                        "RowType": "Industry",
                        "ColCode": "111CA",
                        "ColDescr": "Farms",
                        "ColType": "Commodity",
                        "DataValue": "2.1",
                        "NoteRef": "57",
                    },
                    {
                        "TableID": "57",
                        "Year": "2022",
                        "RowCode": "111CA",
                        "RowDescr": "Farms",
                        "RowType": "Industry",
                        "ColCode": "487OS",
                        "ColDescr": "Other transportation and support activities",
                        "ColType": "Commodity",
                        "DataValue": "0.01",
                        "NoteRef": "57",
                    },
                ],
            }
        ]
    }
}


def _bea_ids(payload, dataset, table):
    parsed = parse_data(payload, dataset=dataset, table=table, table_desc="t", unit_param="TableID")
    return list(parsed.series)


def test_bea_ids_carry_the_industry_code_and_the_official_concordance_maps_it(tables):
    ids = _bea_ids(BEA_GDPI, "GDPbyIndustry", "1")
    assert "GDPbyIndustry:1:111CA:A" in ids  # formato real que produce el adaptador
    links = nx.series_links({("bea", i): "Value added | t" for i in ids}, tables)
    farms = links[("bea", "GDPbyIndustry:1:111CA:A")]
    assert sorted((c, round(w, 2), m) for c, w, m in farms) == [
        ("111", 0.5, "concordancia_bea"),
        ("112", 0.5, "concordancia_bea"),
    ]
    assert links[("bea", "GDPbyIndustry:1:44RT:A")] == [("44-45", 1.0, "concordancia_bea")]
    assert links[("bea", "GDPbyIndustry:1:5415:A")][0][0] == "5415"
    # Agregados que BEA define sin NAICS: sin fila, no se inventa.
    assert ("bea", "GDPbyIndustry:1:PVT:A") not in links
    assert ("bea", "GDPbyIndustry:1:GDP:A") not in links


def test_bea_underlying_and_input_output_ids(tables):
    assert nx.bea_codes("UnderlyingGDPbyIndustry:210:23EH:A") == ["23EH"]
    io = _bea_ids(BEA_IO, "InputOutput", "57")
    assert "InputOutput:57:111CA.Industry.487OS.Commodity:A" in io
    # Se ancla el lado marcado Industry (la fila 111CA), no el producto 487OS.
    assert nx.bea_codes("InputOutput:57:111CA.Industry.487OS.Commodity:A") == ["111CA"]
    # Con el código de fila vacío y solo un producto, se usa el producto.
    assert nx.bea_codes("InputOutput:57:Industry.111CA.Commodity:A") == ["111CA"]
    got = nx.series_links({("bea", i): "t" for i in io}, tables)
    assert all(links[0][2] == "concordancia_bea" for links in got.values()) and len(got) == 2
    assert nx.bea_codes("NIPA:T10101:A191RL:A") == []  # otros datasets no llevan industria


def test_bea_table_is_official_consistent_and_covers_the_main_datasets(tables):
    assert len(tables.bea) > 400
    for code, pairs in tables.bea.items():
        assert all(n in tables.names for n, _ in pairs), code
        assert abs(sum(w for _, w in pairs) - 1) < 1e-3, code
    # Todos los códigos de industria de las filas reales de arriba, salvo los agregados sin NAICS.
    for code in ("111CA", "44RT", "5415", "23EH", "487OS", "311FT", "33DG"):
        assert code in tables.bea, code
    assert (REPO_ROOT / "config" / "naics" / "SOURCES.md").read_text().count("bea.gov") >= 1


CENSUS_IDS = {
    # EITS: la dimensión de industria es `category_code`.
    ("census", "timeseries:eits:mrts:cell_value:category_code=441:data_type_code=SM"): "441",
    ("census", "timeseries:eits:qss:cell_value:category_code=5415:seasonally_adj=yes"): "5415",
    ("census", "timeseries:eits:mrts:cell_value:category_code=44X72:data_type_code=SM"): None,
    ("census", "timeseries:eits:m3:cell_value:category_code=MDM:data_type_code=VS"): None,
    # Encuestas anuales: columna NAICSaaaa.
    ("census", "timeseries:asm:area2017:RCPTPLM:NAICS2017=311:SECTOR=31"): "311",
    ("census", "timeseries:bds:EMP:NAICS=315110"): "315120",  # NAICS 2017 que cambió en 2022
    ("census", "timeseries:qwi:sa:Emp:industry=00:sex=0"): None,  # 00 = todas las industrias
}


def test_census_ids_with_their_own_naics_dimension_are_anchored(tables):
    links = nx.series_links({k: "t" for k in CENSUS_IDS}, tables)
    for key, expected in CENSUS_IDS.items():
        if expected is None:
            assert key not in links, key
        else:
            assert [c for c, _, _ in links[key]] == [expected], key
            assert links[key][0][2] == "codigo_en_id"


def test_census_and_bea_coverage_with_own_code_is_never_zero(tables):
    objects = [*CENSUS_IDS, *(("bea", i) for i in _bea_ids(BEA_GDPI, "GDPbyIndustry", "1"))]
    links = nx.series_links({k: "sin código ni nombre en el título" for k in objects}, tables)
    cov = nx.coverage(objects, links)
    assert cov["census"]["with_naics"] > 0 and cov["bea"]["with_naics"] > 0
    assert cov["census"]["pct"] >= 50 and cov["bea"]["pct"] >= 60


def test_title_name_still_works_when_the_id_has_no_code(tables):
    # Sin código en el id, BEA y Census siguen cayendo al nombre oficial completo del título.
    got = nx.series_links({("bea", "NIPA:T10101:X:A"): "Value added | Retail trade"}, tables)
    assert got[("bea", "NIPA:T10101:X:A")][0][2] == "nombre_oficial"


def test_enumeration_word_does_not_anchor_an_industry(tables):
    # ADR 0025: «Utilities» (NAICS 22) como palabra suelta de una enumeración no ancla nada.
    for title in ("Housing and utilities", "Housing, utilities, and fuels"):
        assert nx.series_links({("fred", "x"): title}, tables) == {}
    got = nx.series_links({("fred", "y"): "Value added | Utilities"}, tables)
    assert [c for c, _, _ in got[("fred", "y")]] == ["22"]


def test_official_name_with_trailing_qualifier_still_anchors(tables):
    title = "Total Revenue for 623: Nursing and Residential Care Facilities, All Establishments"
    got = nx.series_links({("fred", "z"): title}, tables)
    assert [c for c, _, _ in got[("fred", "z")]] == ["623"]
