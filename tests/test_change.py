"""Capa 3, pregunta 1 (ADR 0030): «qué cambió» con datos sintéticos."""

from __future__ import annotations

import json
from datetime import date

import pyarrow as pa
import pytest

from src.config import load_config
from src.rank import change as ch
from src.rank import dimensions as dm
from src.rank import trajectory as traj_mod
from src.schemas import PHENOMENON_CHANGE, SchemaError, validate

P = ch.ChangeParams()
DP = dm.DimParams()
TP = traj_mod.TrajectoryParams()


def market(rows):
    return dm.Market(
        {"naics": n, "metric": m, "year": y, "value": v, "units": "u", "series_id": ""}
        for (n, m, y), v in rows.items()
    )


def ev(sid, kind="cantidad", traj="acelera", **kw):
    return {
        "item_kind": "series", "source": "bls", "object_id": sid, "public": True,
        "measure_kind": kind, "measure_subtype": None, "real_effect": 0.5, "trajectory": traj,
        "window_start": date(2021, 7, 1), "window_end": date(2026, 1, 1), **kw,
    }  # fmt: skip


def phen(naics=(("6231", 1.0),)):
    return {
        "phenomenon_id": "pX", "window_start": date(2021, 1, 1),
        "naics_json": json.dumps([list(x) for x in naics]),
    }  # fmt: skip


def monthly(start_year, years, f):
    ds = [date(start_year + i // 12, i % 12 + 1, 1) for i in range(years * 12)]
    return ds, [f(i / 12) for i in range(len(ds))]


# --- cantidad y recuperación ----------

KEY = ("bls", "A")


def ctx(roles=None, *, series=None, prices=None, deflator=None, naics=None):
    return ch.Ctx(
        "6231", roles if roles is not None else {KEY: "produccion"}, naics or {},
        prices or {}, series or (lambda s, i: None), deflator, "DEFL", {}, TP,
    )  # fmt: skip


def test_quantity_yes_when_production_series_accelerates_or_turns_up():
    for traj in ("acelera", "gira_al_alza"):
        qty, rec = ch.classify_quantity([ev("A", traj=traj)], ctx())
        assert (qty.status, qty.value, rec.status, qty.level) == ("si", 1.0, "no", "directo")


def test_quantity_no_and_recovery_for_frena_caida_and_rebote_bajo_pico():
    for traj in ("frena_caida", "rebote_bajo_pico"):
        qty, rec = ch.classify_quantity([ev("A", traj=traj)], ctx())
        assert (qty.status, qty.value) == ("no", 0.0)
        assert (rec.status, rec.value) == ("si", 1.0) and traj in rec.note


def test_quantity_no_when_trajectory_is_none_or_falling():
    roles = {("bls", "A"): "produccion", ("bls", "B"): "produccion"}
    evid = [ev("A", traj=None), ev("B", traj="gira_a_la_baja")]
    qty, rec = ch.classify_quantity(evid, ctx(roles))
    assert (qty.status, rec.status) == ("no", "no")


def test_inputs_never_count_as_quantity():
    """Empleo, nómina y gastos son insumos: aunque aceleren, no son cantidad producida."""
    roles = {("bls", "A"): "insumo", ("bls", "B"): "sin_clasificar"}
    qty, rec = ch.classify_quantity([ev("A"), ev("B"), ev("C", kind="valor")], ctx(roles))
    assert (qty.status, qty.value, rec.status) == ("sin_dato", None, "sin_dato")


def test_population_and_prices_are_excluded():
    roles = {("bls", "A"): "produccion", ("bls", "P"): "produccion"}
    evid = [ev("A", measure_subtype="poblacion"), ev("P", kind="precio")]
    assert ch.classify_quantity(evid, ctx(roles))[0].status == "sin_dato"


def test_value_series_deflated_by_own_industry_price_index():
    """Ingresos que crecen como los precios: no hay cantidad nueva; si crecen más, sí."""
    start = date(2005, 1, 1)
    months = [date(start.year + i // 12, i % 12 + 1, 1) for i in range(21 * 12)]
    price = (months, [100 * 1.03 ** (i / 12) for i in range(len(months))])
    quarters = [d for d in months if d.month in (1, 4, 7, 10)]

    def run(rate):
        level, vals = 50.0, []
        for d in quarters:  # crecimiento continuo por tramos (sin saltos de nivel)
            level *= (1 + rate(d)) ** 0.25
            vals.append(level)
        rev = (quarters, vals)
        c = ctx(
            {KEY: "produccion"}, series=lambda s, i: price if i == "PCU" else rev,
            prices={"6231": ("bls", "PCU")}, naics={KEY: "6231"},
        )  # fmt: skip
        return ch.classify_quantity([ev("A", "valor")], c)[0]

    flat = run(lambda d: 0.03)  # crece lo mismo que el precio: cantidad constante
    assert flat.status == "no" and flat.level == "directo"
    boom = run(lambda d: 0.03 if d.year < 2022 else 0.09)  # acelera en términos reales
    assert boom.status == "si" and boom.level == "directo"


def test_value_series_level_indirect_when_price_index_is_other_naics_or_deflator():
    months = [date(2005 + i // 12, i % 12 + 1, 1) for i in range(21 * 12)]
    price = (months, [100 * 1.02 ** (i / 12) for i in range(len(months))])
    quarters = [d for d in months if d.month in (1, 4, 7, 10)]
    rev = (quarters, [50 * 1.03 ** ((d.year - 2005) + d.month / 12) for d in quarters])
    other = ctx(
        {KEY: "produccion"}, series=lambda s, i: price if i == "PCU" else rev,
        prices={"6231": ("bls", "PCU")}, naics={KEY: "623"},
    )  # fmt: skip
    got = ch.classify_quantity([ev("A", "valor")], other)[0]
    assert (
        got.level == "indirecto" and "índice de precios de 6231 para una serie de 623" in got.note
    )
    fallback = ctx({KEY: "produccion"}, series=lambda s, i: rev, deflator=price)
    got = ch.classify_quantity([ev("A", "valor")], fallback)[0]
    assert got.level == "indirecto" and "deflactor DEFL" in got.note


def test_quantity_hides_copyrighted_ids_but_counts_them():
    roles = {("bls", "SECRET"): "produccion", ("bls", "OPEN"): "produccion"}
    qty, _ = ch.classify_quantity([ev("SECRET", public=False), ev("OPEN")], ctx(roles))
    assert "SECRET" not in (qty.source_ref or "") + (qty.note or "")
    assert qty.source_ref == "bls:OPEN" and "+1 con derechos" in qty.note


def test_inputs_note_lists_inputs_with_trajectory_for_productivity():
    roles = {("bls", "EMP"): "insumo", ("bls", "REV"): "produccion"}
    note = ch.insumos_note([ev("EMP", traj="rebote_bajo_pico"), ev("REV")], ctx(roles))
    assert note == "insumos: bls:EMP rebote_bajo_pico"


def test_role_table_separates_inputs_from_production():
    from src.rank import role

    rules = role.load_rules()

    def r(title, units=""):
        return role.role_of(rules, source="fred", series_id="X", title=title, units=units)

    assert r("Aggregate Weekly Payrolls of All Employees") == "insumo"
    assert r("Total Operating Expenses") == "insumo"
    assert r("All Employees, Thousands of Persons") == "insumo"
    assert r("Average Weekly Hours") == "insumo"
    assert r("Total Revenue") == "produccion"
    assert r("Retail Sales") == "produccion"
    assert r("Value Added", units="Billions of Chained Dollars") == "produccion"
    assert r("Something unrelated", units="Index") == "sin_clasificar"
    rule = {"id": "x", "role": "otro", "match": {"title_regex": "a"}}
    for bad in ({"version": 1, "rules": [rule]}, {"version": 2, "rules": []},
                {"version": 1, "rules": [{"id": "x"}]}):  # fmt: skip
        with pytest.raises(ValueError):
            role.validate_table(bad)


# --- precio ----------


def price_case(ppi_rate, defl_rate=0.03, prices=None):
    ppi = monthly(2020, 6, lambda t: 100 * (1 + ppi_rate) ** t)
    defl = monthly(2020, 6, lambda t: 100 * (1 + defl_rate) ** t)
    return ch.classify_price(
        "6231", date(2021, 1, 1), {"6231": ("bls", "PCU")} if prices is None else prices,
        lambda s, i: ppi, defl, "DEFL", P,
    )  # fmt: skip


def test_price_yes_when_ppi_beats_deflator_by_more_than_threshold():
    out = price_case(0.06)
    assert out.status == "si" and out.value == pytest.approx(3.0, abs=0.15)


def test_price_no_when_ppi_below_deflator_or_within_threshold():
    assert price_case(0.01).status == "no" and price_case(0.01).value < 0
    assert price_case(0.035).status == "no"  # +0.5 pp/año < 1 pp


def test_price_sin_dato_without_index_or_deflator():
    assert price_case(0.06, prices={}).status == "sin_dato"
    out = ch.classify_price("6231", date(2021, 1, 1), {}, lambda s, i: None, None, None, P)
    assert out.status == "sin_dato" and out.value is None


# --- productividad ----------

DEFL = (
    [date(2017, 6, 1), date(2022, 6, 1)],
    [100.0, 100.0],
)  # deflactor plano: el ingreso real es el nominal


def prod_market(rev1, emp1, cbp1=None):
    rows = {
        ("6231", "revenue", 2017): 1000.0, ("6231", "revenue", 2022): rev1,
        ("6231", "employment_ec", 2017): 100.0, ("6231", "employment_ec", 2022): emp1,
        ("6231", "establishments_ec", 2017): 10.0, ("6231", "establishments_ec", 2022): 10.0,
    }  # fmt: skip
    if cbp1 is not None:
        rows |= {("6231", "employment", 2017): 100.0, ("6231", "employment", 2022): cbp1}
    return market(rows)


def test_productivity_yes_with_falling_or_stable_employment():
    for emp1 in (80.0, 100.0):
        out = ch.classify_productivity("6231", prod_market(1500.0, emp1), DEFL, P)
        assert out.status == "si" and out.value > P.productivity_min_pct_year


def test_productivity_no_when_employment_grows_or_income_flat():
    assert ch.classify_productivity("6231", prod_market(1500.0, 150.0), DEFL, P).status == "no"
    assert ch.classify_productivity("6231", prod_market(1000.0, 100.0), DEFL, P).status == "no"


def test_productivity_uses_cbp_employment_when_available():
    out = ch.classify_productivity("6231", prod_market(1500.0, 80.0, cbp1=150.0), DEFL, P)
    assert out.status == "no" and "employment" in out.source_ref


def test_productivity_deflates_revenue():
    inflated = ([date(2017, 6, 1), date(2022, 6, 1)], [100.0, 150.0])
    out = ch.classify_productivity("6231", prod_market(1500.0, 100.0), inflated, P)
    assert out.status == "no" and out.value == pytest.approx(0.0, abs=1e-9)


def test_productivity_sin_dato():
    assert ch.classify_productivity("6231", market({}), DEFL, P).status == "sin_dato"
    assert ch.classify_productivity(None, prod_market(1500.0, 80.0), DEFL, P).status == "sin_dato"
    assert ch.classify_productivity("6231", prod_market(1500.0, 80.0), None, P).status == "sin_dato"


# --- composición ----------


def test_composition_each_criterion_alone_triggers():
    cases = {
        "subindustrias": {
            ("62311", "employment", 2016): 50.0, ("62311", "employment", 2023): 80.0,
            ("62312", "employment", 2016): 50.0, ("62312", "employment", 2023): 20.0,
        },
        "cr4": {("6231", "cr4", 2017): 10.0, ("6231", "cr4", 2022): 13.0},
        "cr8": {("6231", "cr8", 2017): 10.0, ("6231", "cr8", 2022): 7.0},
        "pequenas": {
            ("6231", "small_firm_share", 2017): 20.0, ("6231", "small_firm_share", 2022): 23.0
        },
        "entrada": {("6231", "entry_rate", 2023): 14.0, ("00", "entry_rate", 2023): 10.0},
        "salida": {("6231", "exit_rate", 2023): 5.0, ("00", "exit_rate", 2023): 9.0},
    }  # fmt: skip
    for name, rows in cases.items():
        out = ch.classify_composition("6231", market(rows), P)
        assert (out.status, out.value) == ("si", 1.0), name


def test_composition_no_when_all_evaluated_below_thresholds():
    rows = {
        ("6231", "cr4", 2017): 10.0, ("6231", "cr4", 2022): 10.5,
        ("6231", "small_firm_share", 2017): 20.0, ("6231", "small_firm_share", 2022): 20.5,
        ("6231", "entry_rate", 2023): 10.5, ("00", "entry_rate", 2023): 10.0,
        ("62311", "employment", 2016): 50.0, ("62311", "employment", 2023): 52.0,
        ("62312", "employment", 2016): 50.0, ("62312", "employment", 2023): 48.0,
    }  # fmt: skip
    out = ch.classify_composition("6231", market(rows), P)
    assert (out.status, out.value) == ("no", 0.0)


def test_composition_sin_dato():
    assert ch.classify_composition("6231", market({}), P).status == "sin_dato"
    assert (
        ch.classify_composition(None, market({("6231", "cr4", 2017): 1.0}), P).status == "sin_dato"
    )


def test_subindustry_shift_ignores_children_missing_in_one_year():
    rows = {
        ("62311", "employment", 2016): 50.0, ("62311", "employment", 2023): 50.0,
        ("62312", "employment", 2016): 50.0, ("62312", "employment", 2023): 50.0,
        ("62313", "employment", 2023): 500.0,  # nuevo código: no entra al peso
    }  # fmt: skip
    assert ch.subindustry_shift(market(rows), "6231", 7)[0] == pytest.approx(0.0)


# --- regulación, frase y tabla ----------


def test_regulation_is_always_sin_dato_with_note():
    out = ch.classify_regulation()
    assert (out.status, out.value) == ("sin_dato", None)
    assert out.note == "falta vincular regulación con industria"


def build(evidence, mk, roles=None):
    c = ctx(roles if roles is not None else {KEY: "produccion"})
    return ch.build_change(phen(), evidence, mk, ctx=c, p=P, dp=DP)


def test_phrase_template_and_buckets():
    rows = build(
        [ev("A", traj="rebote_bajo_pico")],
        market({("6231", "cr4", 2017): 1.0, ("6231", "cr4", 2022): 9.0}),
    )
    text = ch.phrase(rows)
    assert text == (
        "Cambió: composición del mercado · No cambió: cantidad · Recuperación: cantidad · "
        "Sin dato: precio, productividad, regulación (falta vincular regulación con industria)"
    )


def test_phrase_without_any_bucket_uses_dash():
    text = ch.phrase(build([], market({})))
    assert text.startswith("Cambió: — · No cambió: — · Recuperación: — · Sin dato: cantidad")


def test_table_rows_validate_and_reject_invalid():
    rows = build([ev("A")], market({}))
    table = ch.to_table_rows("r1", "pX", rows)
    assert {r["change_type"] for r in table} == {
        "cantidad", "precio", "productividad", "composicion", "regulacion", "recuperacion"
    }  # fmt: skip
    validate(pa.Table.from_pylist(table, schema=PHENOMENON_CHANGE.schema), PHENOMENON_CHANGE)
    for bad in (
        {**table[0], "change_type": "inventado"},
        {**table[0], "status": "quizas"},
        {**table[0], "status": "sin_dato", "value": 1.0},
        {**table[0], "status": "si", "value": None},
    ):
        with pytest.raises(SchemaError):
            validate(
                pa.Table.from_pylist([bad], schema=PHENOMENON_CHANGE.schema), PHENOMENON_CHANGE
            )


def test_render_section_and_line():
    from src.report.render import _change, _change_line

    rows = ch.to_table_rows("r1", "pX", build([ev("A")], market({})))
    data = {"change": {"phrase": rows[0]["phrase"], "rows": rows}}
    html = _change(data)
    assert "¿Qué cambió realmente?" in html and "Regulación" in html and "⚪" in html
    assert rows[0]["phrase"] in _change_line(data)
    assert _change({}) == "" and _change_line({}) == ""


def test_config_keys_present():
    p = ch.params_from(load_config())
    assert p.price_min_pp_year > 0 and p.composition_years >= 1
