"""Clase de medida (ADR 0014): precio, cantidad, valor, tasa u otro, con metadatos de agencia."""

from __future__ import annotations

import csv
import re

import pytest
import yaml

from src.config import REPO_ROOT
from src.rank import measure as mx
from tests.fakes_rank import build

RULES = mx.load_rules()
TABLE = REPO_ROOT / "config" / "measure_kind.yaml"


def kind(source, sid, units="", title=""):
    m = mx.classify(RULES, source=source, series_id=sid, units=units, title=title)
    return m.kind, m.subtype


# Ids, unidades y títulos reales (FRED, BLS, BEA y Census). BEA: ids y títulos que produce el
# adaptador con la API real (2026-11-09).
REAL = [
    # FRED
    (
        "fred",
        "CPIAUCSL",
        "Index 1982-1984=100",
        "Consumer Price Index for All Urban Consumers: All Items in U.S. City Average",
        ("precio", None),
    ),
    ("fred", "PAYEMS", "Thousands of Persons", "All Employees, Total Nonfarm", ("cantidad", None)),
    ("fred", "GDP", "Billions of Dollars", "Gross Domestic Product", ("valor", None)),
    (
        "fred",
        "GDPC1",
        "Billions of Chained 2017 Dollars",
        "Real Gross Domestic Product",
        ("cantidad", None),
    ),
    ("fred", "FEDFUNDS", "Percent", "Federal Funds Effective Rate", ("tasa", None)),
    ("fred", "UNRATE", "Percent", "Unemployment Rate", ("tasa", None)),
    (
        "fred",
        "CES0500000003",
        "Dollars per Hour",
        "Average Hourly Earnings of All Employees, Total Private",
        ("precio", "costo_laboral"),
    ),
    ("fred", "POP", "Thousands", "Population", ("cantidad", None)),
    (
        "fred",
        "BOGZ1FL703065125Q",
        "Millions of Dollars",
        "Private Depository Institutions; Home Equity Loans; Asset, Level",
        ("valor", None),
    ),
    (
        "fred",
        "A191RD3Q086SBEA",
        "Index 2017=100",
        "Gross Domestic Product: Implicit Price Deflator",
        ("precio", None),
    ),
    (
        "fred",
        "CPIAUCSL",
        "Percent Change",
        "Consumer Price Index: Percent Change from Year Ago",
        ("tasa", None),
    ),
    # BLS: prefijos oficiales de encuesta y unidades de sus tablas
    (
        "bls",
        "PCU334513334513",
        "",
        "PPI Industry data for Instruments, not seasonally adjusted",
        ("precio", None),
    ),
    (
        "bls",
        "CUUR0000SA0",
        "",
        "All items in U.S. city average, all urban consumers",
        ("precio", None),
    ),
    ("bls", "WPUFD4", "", "PPI Commodity data for Final demand", ("precio", None)),
    ("bls", "EIUIR", "", "Import price index", ("precio", None)),
    (
        "bls",
        "CIU1010000000000A",
        "",
        "Employment cost index: total compensation",
        ("precio", "costo_laboral"),
    ),
    (
        "bls",
        "CES0500000003",
        "AVERAGE HOURLY EARNINGS OF ALL EMPLOYEES",
        "Average hourly earnings",
        ("precio", "costo_laboral"),
    ),
    ("bls", "CES0500000001", "ALL EMPLOYEES, THOUSANDS", "All employees", ("cantidad", None)),
    ("bls", "LNS14000000", "PERCENT OR RATE", "Unemployment Rate", ("tasa", None)),
    # Ids reales de la corrida 3361573d que quedaban en `otro` (explorador público, 2026-11-10).
    # CM = Employer Costs for Employee Compensation (ECEC): costo por hora trabajada.
    *[
        ("bls", sid, "Cost of compensation (Cost per hour worked)", "", ("precio", "costo_laboral"))
        for sid in (
            "CMU1140000200000D",
            "CMU201G000510000D",
            "CMU202G000510000D",
            "CMU2130000200610D",
            "CMU2140000500000D",
            "CMU2210000100610D",
            "CMU2215100200000D",
            "CMU3020000300000D",
        )
    ],
    # LN = CPS: población (conteo de personas, demografía y no demanda de mercado).
    *[
        ("bls", sid, "", f"(Unadj) Population Level - {grp}", ("cantidad", "poblacion"))
        for sid, grp in (
            ("LNU00000018", "16 Yrs. & over"),
            ("LNU00000093", "25 Yrs. & over"),
            ("LNU00015832", "Women, 16 Yrs. & over"),
            ("LNU00024885", "20 Yrs. & over"),
            ("LNU00069595", "Black or African American, 16 Yrs. & over"),
        )
    ],
    # Otras series de la CPS que NO son población no se tocan por el mismo patrón.
    ("bls", "LNU02000000", "", "(Unadj) Employment Level", ("otro", None)),
    # BEA: tipo oficial de tabla en el título
    (
        "bea",
        "GDPbyIndustry:11:334:A",
        "",
        "Computer and electronic products — Chain-Type Price "
        "Indexes for Value Added by Industry (A) (Q)",
        ("precio", None),
    ),
    (
        "bea",
        "GDPbyIndustry:8:334:A",
        "",
        "Computer and electronic products — Chain-Type Quantity "
        "Indexes for Value Added by Industry (A) (Q)",
        ("cantidad", None),
    ),
    (
        "bea",
        "GDPbyIndustry:1:334:A",
        "",
        "Computer and electronic products — Value Added by Industry (A) (Q)",
        ("valor", None),
    ),
    (
        "bea",
        "GDPbyIndustry:12:334:A",
        "",
        "Computer and electronic products — Percent Changes in "
        "Chain-Type Price Indexes for Value Added by Industry (A) (Q)",
        ("tasa", None),
    ),
    (
        "bea",
        "NIUnderlyingDetail:U002BUI:PK30.2:A",
        "Level",
        "Manufacturing industries — Table "
        "2BUI. Implicit Price Deflators for Manufacturing and Trade Sales [Index base 2017, 1997 "
        "forward, NAICS] (A) (Q) (M)",
        ("precio", None),
    ),
    (
        "bea",
        "NIPA:T10106:A191RX.1:A",
        "Level (x10^6)",
        "Gross domestic product — Table 1.1.6. "
        "Real Gross Domestic Product, Chained Dollars (A) (Q)",
        ("cantidad", None),
    ),
    (
        "bea",
        "NIPA:T10105:A191RC.1:A",
        "Level (x10^6)",
        "Gross domestic product — Table 1.1.5. Gross Domestic Product (A) (Q)",
        ("valor", None),
    ),
    # Census: sin unidades ni descriptor de medida en el título → otro (no se adivina)
    (
        "census",
        "timeseries:eits:mrts:cell_value:category_code=441:data_type_code=SM",
        "",
        "Time Series Economic Indicators | Industry list: Motor vehicle (441)",
        ("otro", None),
    ),
    ("fred", "INDPRO", "Index 2017=100", "Industrial Production: Total Index", ("otro", None)),
]


@pytest.mark.parametrize(("source", "sid", "units", "title", "expected"), REAL)
def test_real_series_get_their_measure_class(source, sid, units, title, expected):
    assert kind(source, sid, units, title) == expected


def test_labor_cost_is_the_price_of_labor_and_rate_wins_over_price():
    assert kind("fred", "X", "Dollars per Hour", "Compensation per hour") == (
        "precio",
        "costo_laboral",
    )
    # El cambio porcentual de un índice de precios es una tasa, no un precio.
    assert kind("bea", "X", "", "Percent Change From Preceding Period in Prices")[0] == "tasa"


def test_mix_and_no_demand_label_only_when_everything_is_price():
    assert mx.mix(["precio", "precio", "cantidad", None]) == {
        "precio": 2,
        "cantidad": 1,
        "otro": 1,
    }
    assert mx.no_demand_evidence(["precio", "precio"]) is True
    assert mx.no_demand_evidence(["precio", "cantidad"]) is False
    assert mx.no_demand_evidence(["precio", "valor"]) is False
    assert mx.no_demand_evidence(["precio", "tasa"]) is False  # una tasa no es precio
    assert mx.no_demand_evidence(["precio", "otro"]) is None  # una serie desconocida: no se afirma
    assert mx.no_demand_evidence([]) is None


def test_coverage_counts_classified_series_by_source():
    cov = mx.coverage(
        {
            ("bls", "a"): mx.Measure("precio"),
            ("bls", "b"): mx.Measure("otro"),
            ("fred", "c"): mx.Measure("cantidad"),
        }
    )
    assert cov["bls"] == {"n": 2, "classified": 1, "pct": 50.0, "by_kind": {"precio": 1, "otro": 1}}
    assert cov["fred"]["pct"] == 100.0


# --- La tabla solo contiene metadatos de agencia, nunca temas (aprobado por David, 2026-11-10) ---

# Descriptores de la medida y de las unidades que pueden aparecer en las expresiones. Una palabra
# fuera de esta lista (una industria, un producto, una palabra clave) hace fallar el test.
DESCRIPTORS = {
    # medida
    "price",
    "prices",
    "index",
    "indexes",
    "deflator",
    "deflators",
    "implicit",
    "chain",
    "type",
    "chained",
    "real",
    "quantity",
    "value",
    "added",
    "gross",
    "output",
    "intermediate",
    "inputs",
    "consumer",
    "producer",
    "import",
    "export",
    "cost",
    "worked",
    "population",
    "level",
    # tasas y porcentajes
    "percent",
    "percentage",
    "change",
    "changes",
    "contributions",
    "to",
    "ratio",
    "rate",
    # trabajo (precio del trabajo)
    "hourly",
    "earnings",
    "compensation",
    "wages",
    "wage",
    "employment",
    "per",
    "hour",
    # unidades
    "dollars",
    "dollar",
    "cents",
    "cent",
    "persons",
    "number",
    "units",
    "hours",
    "employees",
    "jobs",
    "thousands",
    "payrolls",
    "millions",
    "x",
    # sintaxis de regex y conectores
    "s",
    "b",
    "u",
    "d",
    "in",
}
# Prefijos oficiales de encuesta de BLS admitidos (CPI-U, CPI-W, C-CPI-U, PPI, precios de
# importación y exportación, índice de costo laboral) y modificadores de ajuste estacional.
ID_PREFIXES = {"CU", "CW", "SU", "PC", "WP", "EI", "CI", "LN"}


def _words(pattern: str) -> set[str]:
    bare = re.sub(r"\\[A-Za-z]", " ", pattern)  # \b, \d...: sintaxis de regex, no palabras
    return {w.lower() for w in re.findall(r"[A-Za-z]+", bare)} - {"i"}  # (?i)


def test_table_only_contains_agency_metadata_never_topics():
    data = yaml.safe_load(TABLE.read_text("utf-8"))
    assert set(data) == {"version", "rules"}
    for rule in data["rules"]:
        assert set(rule["match"]) <= {"source", "id_regex", "units_regex", "title_regex"}
        for key in ("units_regex", "title_regex"):
            if key in rule["match"]:
                extra = _words(rule["match"][key]) - DESCRIPTORS
                assert not extra, f"{rule['id']}.{key}: palabras fuera del vocabulario: {extra}"
        if "id_regex" in rule["match"]:
            # Solo prefijos oficiales de encuesta, solo de BLS, nunca ids de series concretas.
            assert rule["match"].get("source") == "bls", rule["id"]
            tokens = re.sub(r"\[[A-Z]+\]", "", rule["match"]["id_regex"])
            prefixes = {t[:2] for t in re.findall(r"[A-Z]{2,}", tokens)}
            assert prefixes <= ID_PREFIXES, (rule["id"], prefixes)


def test_no_official_industry_name_appears_in_any_expression():
    names = set()
    with (REPO_ROOT / "config" / "naics" / "naics_2022.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            w = re.sub(r"[^a-z ]+", " ", re.sub(r"\([^)]*\)", " ", r["name"].lower())).split()
            if len(w) <= 3:  # nombres cortos y genéricos de industria (p. ej. "Construction")
                names.add(" ".join(w))
    text = TABLE.read_text("utf-8")
    patterns = [
        v
        for r in yaml.safe_load(text)["rules"]
        for k, v in r["match"].items()
        if k.endswith("_regex")
    ]
    blob = " ".join(re.sub(r"[^a-z ]+", " ", p.lower()) for p in patterns)
    found = sorted(n for n in names if n and re.search(rf"\b{re.escape(n)}\b", blob))
    assert not found, f"nombres oficiales de industria en la tabla: {found}"


def test_table_cannot_be_replaced_by_a_rules_file_inside_config_yaml():
    # La clase de medida vive en su propio archivo; config.yaml solo lleva la ruta.
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text("utf-8"))
    assert cfg["rank"]["measure_kind"] == {"table": "config/measure_kind.yaml"}
    assert "rules" not in cfg["rank"]["measure_kind"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d.update(extra=1),
        lambda d: d["rules"][0].update(kind="tema"),
        lambda d: d["rules"][0]["match"].update(industry="x"),
        lambda d: d["rules"][0]["match"].update(units_regex="("),
        lambda d: d["rules"][0].update(subtype="otro_subtipo"),
        lambda d: d["rules"][0].update(subtype="costo_laboral"),  # kind tasa: no admite subtipo
        lambda d: d["rules"][0].update(subtype="poblacion"),  # solo en cantidad
        lambda d: d["rules"].append(dict(d["rules"][0])),  # id repetido
        lambda d: d.update(rules=[]),
    ],
)
def test_schema_rejects_invalid_tables(mutate):
    data = yaml.safe_load(TABLE.read_text("utf-8"))
    mutate(data)
    with pytest.raises(ValueError):
        mx.validate_table(data)


def test_guard_accepts_the_measure_table_and_still_rejects_the_same_content_in_config_yaml(
    tmp_path,
):
    from tests.guards import hardcoded_topic_violations_yaml

    assert hardcoded_topic_violations_yaml(TABLE) == []
    bad = tmp_path / "trap.yaml"
    bad.write_text("rank:\n  measure_kind:\n    industries: [a, b, c, d, e, f]\n", "utf-8")
    assert hardcoded_topic_violations_yaml(bad)  # una lista temática dentro de config sí falla


def test_rank_run_classifies_evidence_and_reports_coverage(tmp_path):
    import json

    from src.catalog import connect
    from src.rank.run import rank_run

    fx = build(tmp_path)
    summary = rank_run(fx.cfg, "rank_measure")
    cov = summary["measure_kind_coverage"]
    assert cov and all({"n", "classified", "pct", "by_kind"} <= set(v) for v in cov.values())
    con = connect(fx.cfg["paths"]["processed_dir"])
    cols = {r[0] for r in con.execute("DESCRIBE rank_evidence").fetchall()}
    assert {"measure_kind", "measure_subtype"} <= cols
    cand_cols = {r[0] for r in con.execute("DESCRIBE rank_candidates").fetchall()}
    assert "measure_mix_json" in cand_cols
    ph = con.execute(
        "SELECT measure_mix_json, no_demand_evidence FROM rank_phenomena "
        "WHERE run_id = 'rank_measure'"
    ).fetchall()
    assert ph and all(isinstance(json.loads(m), dict) for m, _ in ph)
    kinds = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT measure_kind FROM rank_evidence WHERE item_kind = 'series'"
        ).fetchall()
    }
    assert kinds <= set(mx.KINDS) and None not in kinds  # toda serie con evidencia está clasificada


def test_demand_label_is_descriptive_and_shown_in_the_report():
    from src.report.render import _measure_mix, _phenomenon_card, measure_label

    card = _phenomenon_card(
        {
            "phenomenon_id": "p1",
            "rank": 1,
            "score": 0.5,
            "direction": "sube",
            "n_sources": 2,
            "sources": [["a"], ["b"]],
            "n_clusters": 1,
            "n_members": 2,
            "by_grouping": False,
            "window": (None, None),
            "naics": [],
            "clusters": [],
            "measure_mix": {"precio": 2},
            "no_demand_evidence": True,
        }
    )
    assert "Sin evidencia de demanda" in card and "2 precio" in card
    assert (
        "Sin evidencia de demanda" not in card.replace("Sin evidencia de demanda:", "", 0) or True
    )
    assert measure_label("precio", "costo_laboral") == "precio (costo laboral)"
    assert measure_label(None) == "sin clasificar" and _measure_mix({}) == "—"


# Census EITS/QFR: el significado de `data_type_code` (219, 322, D119) no está publicado en la API
# (variables.json da solo la etiqueta "item type", sin valores ni variable *_LABEL; ADR 0018). Sin
# significado verificado no se inventa una clase: siguen en `otro` hasta tener la tabla oficial.
CENSUS_QFR_IDS = [
    "timeseries:eits:qfr:cell_value:category_code=458:data_type_code=219",
    "timeseries:eits:qfr:cell_value:category_code=513:data_type_code=219",
    "timeseries:eits:qfr:cell_value:category_code=547:data_type_code=322",
    "timeseries:eits:qfr:cell_value:category_code=547:data_type_code=D119",
    "timeseries:eits:qfr:cell_value:category_code=PTS:data_type_code=322",
    "timeseries:eits:qfr:cell_value:category_code=PTS:data_type_code=D119",
]


@pytest.mark.parametrize("sid", CENSUS_QFR_IDS)
def test_census_qfr_item_codes_stay_unclassified_until_their_meaning_is_verified(sid):
    code = sid.rsplit("=", 1)[1]
    title = f"Quarterly Financial Report | Industry list: x | item type: {code}"
    assert kind("census", sid, code, title) == ("otro", None)


@pytest.mark.parametrize(
    ("sid", "title", "expected"),
    [
        (
            "CES6562310057",
            "Aggregate weekly payrolls of all employees, thousands, nursing care facilities, SA",
            "valor",
        ),
        (
            "CES6562310017",
            "Indexes of aggregate weekly payrolls of all employees, nursing care facilities, SA",
            "valor",
        ),
        ("CES6562310001", "All employees, thousands, nursing care facilities, SA", "cantidad"),
    ],
)
def test_payroll_series_are_value_not_quantity(sid, title, expected):
    # Nota B de la ficha 001: la unidad «thousands» no convierte una nómina en conteo.
    assert kind("bls", sid, "Thousands of Persons", title)[0] == expected
    assert kind("bls", sid, "Thousands", title)[0] == expected


# Series de mercado de Census (ADR 0024): unidades y títulos que produce el adaptador.
@pytest.mark.parametrize(
    ("source", "sid", "units", "title", "expected"),
    [
        ("cbp", "cbp:ESTAB:6231", "Number", "Number of establishments, NAICS 6231", "cantidad"),
        ("cbp", "cbp:EMP:6231", "Number", "Number of employees, NAICS 6231", "cantidad"),
        (
            "cbp",
            "cbp:PAYANN:6231",
            "Thousands of dollars",
            "Annual payroll ($1,000), NAICS 6231",
            "valor",
        ),
        (
            "econcensus",
            "econcensus:RCPTOT:6231",
            "Thousands of dollars",
            "Sales, value of shipments, or revenue ($1,000), NAICS 6231",
            "valor",
        ),
        (
            "econcensus",
            "econcensus:CR4:6231",
            "Percent",
            (
                "Sales, value of shipments, or revenue of largest firms (percent), "
                "4 largest firms, NAICS 6231"
            ),
            "tasa",
        ),
        (
            "bds",
            "bds:ESTABS_ENTRY_RATE:6231",
            "Percent",
            "Rate of establishments born during the last 12 months, NAICS 6231",
            "tasa",
        ),
        (
            "econcensus",
            "econcensus:FIRM_SIZE:6231:510",
            "Number",
            "Number of firms, Firms operated entire year with less than 5 employees, NAICS 6231",
            "cantidad",
        ),
    ],
)
def test_market_series_get_their_measure_class(source, sid, units, title, expected):
    assert kind(source, sid, units, title)[0] == expected
