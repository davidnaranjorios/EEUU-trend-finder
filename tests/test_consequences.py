"""Capa 3, pregunta 2 (ADR 0032): consecuencias con reglas fijas, sobre datos sintéticos."""

from __future__ import annotations

import json
from datetime import date

import pyarrow as pa
import pytest

from src.market.build import IO_FLOWS, build_io_flows
from src.rank import consequences as cs
from src.rank import dimensions as dm
from src.schemas import PHENOMENON_CONSEQUENCES, SchemaError, validate

P = cs.ConsParams()
DP = dm.DimParams() if hasattr(dm, "DimParams") else None
PHEN = {"naics_json": json.dumps([["6231", 1.0]])}


def io_row(
    frm, to, value, *, fk="industria", tk="industria", cat=None, year=2023, fn=None, tn=None
):
    return {
        "year": year,
        "from_industry": frm,
        "to_industry": to,
        "from_desc": f"desc {frm}",
        "to_desc": f"desc {to}",
        "from_kind": fk,
        "to_kind": tk,
        "demand_category": cat,
        "from_naics": fn,
        "to_naics": tn,
        "value": float(value),
        "units": "Millions of dollars",
        "source": "bea_io",
        "series_id": f"s:{frm}:{to}",
    }


def io_flows():
    """Industria BEA «AAA» (NAICS 623 → ancestro de 6231): vende 100 a hogares, 10 a gobierno,
    5 a inversión, 0 a exportaciones; compradoras BBB 30 y CCC 20 (+ DDD con valor 0); compra a
    BBB 40, CCC 10 y a sí misma 50 (la propia industria cuenta como proveedor)."""
    f = {"fn": "623"}
    rows = [
        io_row("AAA", "F1", 100, tk="demanda_final", cat="hogares", **f),
        io_row("AAA", "F2", 10, tk="demanda_final", cat="gobierno", **f),
        io_row("AAA", "F3", 5, tk="demanda_final", cat="inversion", **f),
        io_row("AAA", "BBB", 30, tn="b", **f),
        io_row("AAA", "CCC", 20, tn="c", **f),
        io_row("AAA", "DDD", 0, **f),
        io_row("AAA", "T019", 165, tk="total", **f),
        io_row("AAA", "T001", 50, tk="total", **f),
        io_row("BBB", "AAA", 40, fn="b", tn="623"),
        io_row("CCC", "AAA", 10, fn="c", tn="623"),
        io_row("AAA", "AAA", 50, tn="623", **f),
        io_row("BBB", "F4", 1, fn="b", tk="demanda_final", cat="exportaciones"),
        io_row("T005", "AAA", 100, fk="total", tn="623"),
    ]
    return cs.IoFlows(rows)


def market(rows):
    return dm.Market(
        {"naics": n, "metric": m, "year": y, "value": v, "units": u, "series_id": ""}
        for (n, m, y, u), v in rows.items()
    )


def by(rows, metric):
    return next(r for r in rows if r.metric == metric)


def build(**kw):
    args = dict(
        io=io_flows(),
        market=market({}),
        series=lambda s, i: None,
        by_naics={},
        change_rows=None,
        text=None,
    )
    args.update(kw)
    dp = dm.params_from({"rank": {"dimensions": _DIMS}})
    return cs.build_consequences(PHEN, args.pop("market"), dp=dp, p=P, **args)


_DIMS = {
    "naics_min_weight_share": 0.5,
    "concentration_ratios": [4, 8, 20, 50],
    "risk_section_regex": "(?i)item[\\s_]*1a",
    "risk_max_items": 5,
    "horizon_years": 7,
}


# --- compradores y proveedores -------------------------------------------------------------------


def test_buyers_shares_of_total_use_and_top_industries():
    rows = [r for r in build() if r.consequence == "compradores"]
    cat = {r.metric: r for r in rows}
    assert cat["demanda_hogares"].value == pytest.approx(100 * 100 / 165)
    assert cat["demanda_gobierno"].value == pytest.approx(100 * 10 / 165)
    assert cat["demanda_exportaciones"].value == 0.0  # categoría de la tabla sin celda para AAA
    assert cat["comprador_1"].value == pytest.approx(100 * 50 / 165)  # AAA se compra a sí misma
    assert "BEA AAA" in cat["comprador_1"].value_text
    assert [r.metric for r in rows if r.metric.startswith("comprador_")] == [
        "comprador_1",
        "comprador_2",
        "comprador_3",
    ]  # DDD (valor 0) no es comprador
    # NAICS 623 es el ancestro de 6231: nivel indirecto y con nota; periodo y fuente
    assert {r.level for r in rows} == {"indirecto"} and cat["comprador_1"].period == "2023"
    assert cat["comprador_1"].source_ref.startswith("market/io_flows")
    assert "contiene a 6231" in cat["comprador_1"].note


def test_providers_share_of_industry_inputs():
    rows = [r for r in build() if r.consequence == "proveedores"]
    assert [r.metric for r in rows] == ["proveedor_1", "proveedor_2", "proveedor_3"]
    assert rows[0].value == pytest.approx(100 * 50 / 100)  # AAA (la misma) 50 de 100
    assert rows[1].value == pytest.approx(100 * 40 / 100)
    assert "BEA BBB" in rows[1].value_text and rows[1].unit.startswith("% de los insumos")


def test_io_levels_direct_when_naics_matches_exactly():
    io = cs.IoFlows(
        [
            io_row("AAA", "F1", 10, tk="demanda_final", cat="hogares", fn="6231"),
            io_row("AAA", "T019", 10, tk="total", fn="6231"),
        ]
    )
    rows = [r for r in build(io=io) if r.consequence == "compradores"]
    assert {r.level for r in rows if r.value is not None} == {"directo"}
    assert rows[0].note is None


def test_buyers_and_providers_sin_dato_without_table_or_concordance():
    for io in (None, cs.IoFlows([io_row("ZZZ", "T019", 5, tk="total", fn="999")])):
        rows = build(io=io)
        assert by(rows, "demanda_final" if io else "demanda_final").level == "sin_dato"
        assert by(rows, "proveedor_1").level == "sin_dato"


def test_most_specific_bea_code_wins_when_several_map_to_the_naics():
    io = cs.IoFlows(
        [
            io_row("WIDE", "F1", 1, tk="demanda_final", cat="hogares", fn="623"),
            io_row("WIDE", "T019", 1, tk="total", fn="623"),
            io_row("WIDE", "X", 1, fn="623"),
            io_row("NARROW", "F1", 2, tk="demanda_final", cat="hogares", fn="623"),
            io_row("NARROW", "T019", 4, tk="total", fn="623"),
        ]
    )
    io.naics_of["WIDE"].update({"111", "112"})  # un código BEA que reúne varios NAICS
    codes, used = io.codes_for("6231")
    assert codes == ["NARROW"] and used == "623"


# --- escasez de capacidad ------------------------------------------------------------------------


def test_payroll_per_worker_against_the_economy_with_units():
    mk = market(
        {
            ("6231", "payroll", 2023, "Number"): 400_000.0,
            ("6231", "employment", 2023, "Number"): 10_000.0,
            ("00", "payroll", 2023, "Number"): 8_000_000_000.0,
            ("00", "employment", 2023, "Number"): 100_000_000.0,
        }
    )
    rows = build(market=mk)
    assert by(rows, "nomina_por_trabajador").value == pytest.approx(40_000.0)
    assert by(rows, "nomina_vs_economia").value == pytest.approx(0.5)  # 40 000 / 80 000
    assert by(rows, "nomina_vs_economia").level == "directo"
    assert by(rows, "nomina_vs_economia").period == "2023"


def test_payroll_sin_dato_without_economy_or_employment():
    mk = market({("6231", "payroll", 2023, "Thousands of dollars"): 5.0})
    assert by(build(market=mk), "nomina_vs_economia").level == "sin_dato"


def test_real_output_per_worker_comes_from_what_changed():
    ch = {
        "productividad": {
            "status": "si",
            "value": 2.5,
            "unit": "% por año",
            "period": "2017 a 2022",
            "source_ref": "market/naics_metrics:revenue",
            "level": "indirecto",
            "note": "ingreso real por trabajador +2.5 %",
        }
    }
    r = by(build(change_rows=ch), "produccion_real_por_trabajador")
    assert (r.value, r.level, r.period) == (2.5, "indirecto", "2017 a 2022")
    sd = {"productividad": {"status": "sin_dato", "value": None, "level": "sin_dato"}}
    assert by(build(change_rows=sd), "produccion_real_por_trabajador").level == "sin_dato"
    assert by(build(change_rows=None), "produccion_real_por_trabajador").level == "sin_dato"


def test_vacancies_found_by_code_not_by_list():
    ref = cs.SeriesRef("bls", "JOBS1", "Job Openings: Health Care", True)
    other = cs.SeriesRef("bls", "OTHER", "Hires: Health Care", True)
    data = {("bls", "JOBS1"): ([date(2024, 1, 1), date(2024, 2, 1)], [100.0, 120.0])}
    r = by(build(by_naics={"62": [other, ref]}, series=lambda s, i: data.get((s, i))), "vacantes")
    assert (r.value, r.level, r.naics_used, r.period) == (120.0, "indirecto", "62", "2024-02")
    assert (
        by(build(by_naics={"72": [ref]}, series=lambda s, i: data.get((s, i))), "vacantes").level
        == "sin_dato"
    )
    assert "JOLTS" in by(build(), "vacantes").note


def test_copyrighted_vacancies_hide_the_title_but_not_the_figure():
    ref = cs.SeriesRef("fred", "X1", "Job Openings: Secreto", False)
    data = {("fred", "X1"): ([date(2024, 1, 1)], [7.0])}
    r = by(build(by_naics={"6231": [ref]}, series=lambda s, i: data.get((s, i))), "vacantes")
    assert r.value == 7.0 and r.public is False and r.value_text is None and r.level == "directo"


# --- pérdida de participación y márgenes -------------------------------------------------------


def test_exit_rate_against_the_economy_and_cr4_change():
    mk = market(
        {
            ("6231", "exit_rate", 2023, "Percent"): 7.317,
            ("00", "exit_rate", 2023, "Percent"): 9.396,
            ("6231", "cr4", 2017, "Percent"): 20.0,
            ("6231", "cr4", 2022, "Percent"): 23.5,
        }
    )
    rows = build(market=mk)
    s = by(rows, "salida_vs_economia")
    assert s.value == pytest.approx(7.317 / 9.396) and "7.317 % frente a 9.396 %" in s.value_text
    assert s.level == "directo" and s.period == "2023"
    c = by(rows, "cambio_cr4")
    assert c.value == pytest.approx(3.5) and c.period == "2017 a 2022"


def test_participation_sin_dato_without_market_rows():
    rows = build()
    assert by(rows, "salida_vs_economia").level == "sin_dato"
    assert by(rows, "cambio_cr4").level == "sin_dato"


def test_margin_needs_revenue_and_expenses_of_the_same_naics_and_scope():
    rev = cs.SeriesRef("fred", "REV", "Total Revenue for 623: Nursing, All Establishments", True)
    exp = cs.SeriesRef("fred", "EXP", "Total Expenses for 623: Nursing, All Establishments", True)
    lone = cs.SeriesRef("fred", "REV2", "Total Revenue for 623: Otros", True)
    d = [date(2023, 10, 1), date(2024, 1, 1)]
    data = {("fred", "REV"): (d, [100.0, 120.0]), ("fred", "EXP"): (d, [90.0, 100.0]),
            ("fred", "REV2"): (d, [1.0, 1.0])}  # fmt: skip
    r = by(build(by_naics={"623": [rev, lone, exp]}, series=lambda s, i: data.get((s, i))),
           "ingresos_menos_gastos")  # fmt: skip
    assert (r.value, r.level, r.period) == (20.0, "indirecto", "2024-01")
    assert "fred:REV" in r.source_ref and "+16.7 %" in r.value_text
    only_rev = build(by_naics={"623": [rev]}, series=lambda s, i: data.get((s, i)))
    m = by(only_rev, "ingresos_menos_gastos")
    assert m.level == "sin_dato" and m.value is None


# --- problemas declarados: sobrerrepresentación ------------------------------------------------


def text_ctx(*, companies=8, docs=40, term_docs=10, all_docs_with=100, total=1000, sic_docs=None):
    """NAICS 6231 = SIC 8051 (peso 1). 40 documentos de ese NAICS de 1000 en total."""
    sic = {"8051": [("6231", 1.0)], "7370": [("5415", 1.0)]}
    tc = cs.TermCounts(all_docs_with, total, sic_docs or {"8051": term_docs}, "2022Q1..2024Q4")
    return cs.TextCtx(
        sic,
        lambda w: ({"8051": docs, "7370": 960}, {"8051": companies, "7370": 300}),
        {"staff turnover": tc},
    )


def test_overrepresentation_is_the_ratio_of_proportions():
    # 10/40 = 25 % de los documentos del NAICS frente a 100/1000 = 10 % en general → 2.5×
    (o,), ev = cs.overrepresented("6231", text_ctx(), P)
    assert ev and o.ratio == pytest.approx(2.5)
    assert (o.pct_naics, o.pct_all, o.docs, o.companies) == (25.0, 10.0, 10.0, 8)


def test_terms_below_the_threshold_are_not_listed_and_give_sin_riesgos_propios():
    ctx = text_ctx(term_docs=6)  # 15 % frente a 10 % → 1.5× < 2×
    assert cs.overrepresented("6231", ctx, P)[0] == []
    (r,) = [x for x in build(text=ctx) if x.consequence == "problemas"]
    assert r.level == "sin_dato" and r.note == "sin riesgos propios de la industria"


def test_min_companies_and_min_docs_are_respected():
    assert cs.overrepresented("6231", text_ctx(companies=4), P) == ([], False)
    assert cs.overrepresented("6231", text_ctx(term_docs=2), P)[0] == []  # < min_docs_naics
    (r,) = [x for x in build(text=text_ctx(companies=4)) if x.consequence == "problemas"]
    assert r.level == "sin_dato" and "menos de 5 empresas" in r.note


def test_problem_rows_carry_value_unit_period_source_and_level():
    rows = [r for r in build(text=text_ctx()) if r.consequence == "problemas"]
    r = rows[0]
    assert r.metric == "termino_1" and r.value == pytest.approx(2.5)
    assert r.level == "directo" and r.period == "2022Q1..2024Q4"
    assert r.source_ref == "edgar:item_1a|staff turnover" and "25.0 %" in r.value_text


def test_text_climbs_to_the_parent_naics_when_the_naics_has_few_companies():
    sic = {"8051": [("6231", 1.0)], "8060": [("6221", 1.0)]}
    tc = cs.TermCounts(100, 1000, {"8051": 4, "8060": 6}, "2022Q1..2024Q4")
    ctx = cs.TextCtx(
        sic, lambda w: ({"8051": 15, "8060": 25}, {"8051": 3, "8060": 5}), {"staff turnover": tc}
    )
    (r,) = [x for x in build(text=ctx) if x.consequence == "problemas"]
    assert r.level == "indirecto" and r.naics_used == "62" and r.value == pytest.approx(2.5)


def test_problems_sin_dato_without_text():
    (r,) = [x for x in build(text=None) if x.consequence == "problemas"]
    assert r.level == "sin_dato" and "sin datos de texto" in r.note


# --- tabla, esquema y portada ------------------------------------------------------------------


def test_table_rows_validate_and_are_unique():
    rows = build(text=text_ctx())
    out = cs.to_table_rows("run1", "p1", rows)
    table = pa.Table.from_pylist(out, schema=PHENOMENON_CONSEQUENCES.schema)
    validate(table, PHENOMENON_CONSEQUENCES)
    assert len({(r["consequence"], r["metric"]) for r in out}) == len(out)
    firsts = [r for r in out if r["consequence"] == "compradores"]
    assert [r["position"] for r in firsts] == list(range(1, len(firsts) + 1))


def test_schema_rejects_bad_rows():
    base = cs.to_table_rows("r", "p", [cs.Row("compradores", "m", 1.0, level="directo")])[0]
    for patch in (
        {"consequence": "inventada"},
        {"level": "raro"},
        {"level": "sin_dato"},  # con valor
        {"value": None, "level": "directo"},  # sin valor ni texto
    ):
        t = pa.Table.from_pylist([{**base, **patch}], schema=PHENOMENON_CONSEQUENCES.schema)
        with pytest.raises(SchemaError):
            validate(t, PHENOMENON_CONSEQUENCES)


def test_headline_names_main_buyer_and_largest_consequence():
    mk = market(
        {
            ("6231", "payroll", 2023, "Number"): 400_000.0,
            ("6231", "employment", 2023, "Number"): 10_000.0,
            ("00", "payroll", 2023, "Number"): 8_000_000_000.0,
            ("00", "employment", 2023, "Number"): 100_000_000.0,
        }
    )
    out = cs.to_table_rows("r", "p", build(market=mk, text=text_ctx()))
    line = cs.headline(out)
    assert line.startswith("comprador principal: hogares (60.6 % del uso)")
    assert "mayor consecuencia: término propio «staff turnover» 2.5×" in line  # |2.5−1| > |0.5−1|
    assert cs.headline([r for r in out if r["consequence"] != "compradores"]) is None


# --- construcción de market/io_flows -----------------------------------------------------------


def _series(cells):
    ser = [
        {"source": "bea_io", "series_id": sid, "date": date(y, 1, 1), "value": v}
        for sid, y, v in cells
    ]
    return pa.Table.from_pylist(ser)


def _meta(titles):
    return pa.Table.from_pylist(
        [{"source": "bea_io", "series_id": s, "title": t} for s, t in titles.items()]
    )


def test_build_io_flows_classifies_rows_columns_and_demand():
    sid = lambda r, c: f"InputOutput:259:{r}.Commodity.{c}.Industry:A"  # noqa: E731
    titles = {
        sid("AAA", "AAA"): "Alpha → Alpha — T",
        sid("AAA", "FPC"): "Alpha → Personal consumption expenditures — T",
        sid("AAA", "FGV"): "Alpha → State and local: Gross investment in equipment — T",
        sid("AAA", "FXP"): "Alpha → Exports of goods and services — T",
        sid("AAA", "FIV"): "Alpha → Private fixed investment in structures — T",
        sid("AAA", "TOT"): "Alpha → Total use of products — T",
        sid("VA1", "AAA"): "Compensation of employees → Alpha — T",
        sid("TIN", "AAA"): "Total Intermediate → Alpha — T [TIN.Commodity.AAA.Industry]",
    }
    cells = [(s, 2023, 10.0) for s in titles] + [(sid("AAA", "AAA"), 2022, 5.0)]
    rows = build_io_flows(
        _series(cells),
        _meta(titles),
        bea_naics={"AAA": ["6231"]},
        demand={
            "hogares": "(?i)^personal consumption",
            "gobierno": "(?i)state and local",
            "inversion": "(?i)investment",
            "exportaciones": "(?i)^exports",
        },  # fmt: skip
        total_regex="(?i)^total",
        units="Millions of dollars",
    )
    pa_table = pa.Table.from_pylist(rows, schema=IO_FLOWS.schema)
    assert pa_table.num_rows == len(rows) == 9  # (2023: 8 celdas) + (2022: 1)
    got = {(r["year"], r["from_industry"], r["to_industry"]): r for r in rows}
    assert got[(2023, "AAA", "AAA")]["to_kind"] == "industria"
    assert (
        got[(2023, "AAA", "AAA")]["from_naics"] == got[(2023, "AAA", "AAA")]["to_naics"] == "6231"
    )
    cats = {c: got[(2023, "AAA", c)]["demand_category"] for c in ("FPC", "FGV", "FXP", "FIV")}
    assert cats == {"FPC": "hogares", "FGV": "gobierno", "FXP": "exportaciones", "FIV": "inversion"}
    assert got[(2023, "AAA", "TOT")]["to_kind"] == "total"
    assert got[(2023, "VA1", "AAA")]["from_kind"] == "otro"
    assert got[(2023, "TIN", "AAA")]["from_kind"] == "total"
    assert (
        got[(2023, "AAA", "FPC")]["from_naics"] == "6231"
        and got[(2023, "AAA", "FPC")]["to_naics"] is None
    )
    assert got[(2023, "AAA", "AAA")]["from_desc"] == "Alpha"


# --- reporte -----------------------------------------------------------------------------------


def test_report_section_hides_copyrighted_rows_and_renders_levels(monkeypatch):
    from src.report import data as rdata
    from src.report import render

    rows = cs.to_table_rows(
        "r",
        "p1",
        [
            cs.Row("compradores", "demanda_hogares", 60.0, unit="%", level="directo"),
            cs.Row(
                "escasez", "vacantes", 5.0, source_ref="fred:SECRETA", level="directo", public=False
            ),
            cs.Row(
                "problemas",
                "terminos_propios",
                level="sin_dato",
                note="sin riesgos propios de la industria",
            ),
        ],
    )
    monkeypatch.setattr(rdata, "_rank_rows", lambda *a, **k: rows)
    out = rdata.consequences_data({}, {"run_id": "r"})["p1"]
    assert [r["metric"] for r in out["rows"]] == ["demanda_hogares", "terminos_propios"]
    assert out["line"].startswith("comprador principal: hogares")
    html = render._consequences({"consequences": out}) + render._conseq_line({"consequences": out})
    assert "¿Qué consecuencias genera?" in html and "✅" in html and "⚪" in html
    assert "SECRETA" not in html and "sin riesgos propios de la industria" in html
    assert render._consequences({"consequences": None}) == ""
