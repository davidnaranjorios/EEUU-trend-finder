"""Capa 2 (ADR 0029): las 9 dimensiones como tabla de cifras, con datos sintéticos."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.config import REPO_ROOT, load_config
from src.rank import dimensions as dm
from src.rank import naics as naics_mod
from src.rank import trajectory as traj
from src.schemas import PHENOMENON_DIMENSIONS, SchemaError, validate

P = dm.DimParams()
TP = traj.TrajectoryParams()


def market(rows: dict[tuple[str, str, int], float]) -> dm.Market:
    return dm.Market(
        {"naics": n, "metric": m, "year": y, "value": v, "units": "u", "series_id": ""}
        for (n, m, y), v in rows.items()
    )


def ev(sid: str, kind: str = "cantidad", **kw):
    return {
        "item_kind": "series", "source": "bls", "object_id": sid, "q_value": 0.02,
        "effect_size": 1.5, "public": True, "window_start": date(2021, 7, 1),
        "window_end": date(2026, 1, 1), "measure_kind": kind, "trajectory": "acelera",
        "real_effect": 0.9, **kw,
    }  # fmt: skip


def phen(naics=(("6231", 0.9), ("623", 0.9))):
    return {
        "phenomenon_id": "pX", "direction": "sube", "trajectory": "acelera",
        "window_start": date(2021, 7, 1), "window_end": date(2026, 1, 1),
        "naics_json": json.dumps([list(x) for x in naics]),
    }  # fmt: skip


def build(ph, evid, mk, **kw):
    args = dict(
        series=lambda s, i: None, deflator=None, deflator_id=None, prices={},
        recession_months=[], traj_params=TP, terms=[], p=P,
    )  # fmt: skip
    args.update(kw)
    return dm.build_phenomenon_rows(ph, evid, mk, **args)


def get(rows, dim, metric):
    return next(r for r in rows if r.dimension == dim and r.metric == metric)


def test_pick_naics_most_specific_among_top_weights():
    assert dm.pick_naics([["623", 0.9], ["6231", 0.9], ["62", 0.2]], 0.5) == "6231"
    assert dm.pick_naics([["623", 1.0], ["6231", 0.2]], 0.5) == "623"
    assert dm.pick_naics([], 0.5) is None


def test_market_dimensions_direct_values():
    mk = market(
        {
            ("6231", "firms", 2022): 10663, ("6231", "establishments", 2023): 18126,
            ("6231", "cr4", 2022): 6.8, ("6231", "revenue", 2022): 135396,
            ("6231", "establishments_ec", 2022): 17951, ("6231", "entry_rate", 2023): 9.025,
            ("00", "entry_rate", 2023): 10.6, ("6231", "exit_rate", 2023): 7.3,
            ("00", "exit_rate", 2023): 9.4,
        }
    )  # fmt: skip
    rows = build(phen(), [ev("A")], mk)
    r = get(rows, "competencia", "empresas")
    assert (r.value, r.period, r.level, r.naics_used) == (10663, "2022", "directo", "6231")
    assert get(rows, "competencia", "establecimientos").period == "2023"
    assert get(rows, "competencia", "cr4").value == 6.8
    assert get(rows, "dinero", "ingresos_totales").value == 135396
    assert get(rows, "dinero", "ingreso_por_establecimiento").value == pytest.approx(135396 / 17951)
    assert get(rows, "entrada", "entry_rate").value == 9.025
    assert get(rows, "entrada", "entry_rate_economia_total").value == 10.6
    assert get(rows, "entrada", "entry_rate_menos_economia").value == pytest.approx(9.025 - 10.6)


def test_naics_level_climbs_and_marks_indirect():
    mk = market({("623", "firms", 2022): 5000})  # solo 3 dígitos: el fenómeno es 6231
    rows = build(phen(), [ev("A")], mk)
    r = get(rows, "competencia", "empresas")
    assert (r.level, r.naics_used, r.value) == ("indirecto", "623", 5000)
    # lo que no existe en ningún nivel queda sin dato (⚪), sin valor
    s = get(rows, "competencia", "cr8")
    assert s.level == "sin_dato" and s.value is None and s.value_text is None


def test_missing_everything_is_blank_not_invented():
    rows = build(phen(()), [], market({}))
    assert get(rows, "fenomeno", "industria").level == "sin_dato"
    assert get(rows, "competencia", "empresas").level == "sin_dato"
    assert get(rows, "brecha", "precio_industria_vs_deflactor").level == "sin_dato"
    assert get(rows, "riesgo", "terminos_item_1a").level == "sin_dato"
    assert get(rows, "confianza", "n_sin_dato").value >= 8


def test_confidence_counts_levels_of_the_other_rows():
    mk = market({("623", "firms", 2022): 5000, ("6231", "cr4", 2022): 6.8})
    rows = build(phen(), [ev("A")], mk)
    body = [r for r in rows if r.dimension != "confianza"]
    for lv in dm.LEVELS:
        assert get(rows, "confianza", f"n_{lv}").value == sum(r.level == lv for r in body)


def monthly(start_year, n, f):
    out = []
    for i in range(n):
        y, m = divmod(start_year * 12 + i, 12)
        out.append((date(y, m + 1, 1), f(i)))
    return [d for d, _ in out], [v for _, v in out]


def test_price_gap_against_deflator_via_naics_in_id():
    tables = naics_mod.load_tables(load_config(), REPO_ROOT)
    prices = dm.find_price_series(
        [("bls", "PCU623110623110"), ("bls", "PCU6231106231101"), ("bls", "OTHER")], tables
    )
    assert prices == {"623110": ("bls", "PCU623110623110")}  # solo el agregado de la industria
    dates, ppi = monthly(2018, 96, lambda i: 100 * 1.002**i)  # +0,2 %/mes
    _, defl = monthly(2018, 96, lambda i: 100 * 1.004**i)  # +0,4 %/mes
    rec = [(2020 * 12 + 1, 1.0), (2020 * 12 + 3, 0.0)]
    rows = build(
        phen((("623110", 1.0),)),
        [ev("A")],
        market({}),
        series=lambda s, i: (dates, ppi),
        deflator=(dates, defl),
        deflator_id="DEF",
        prices=prices,
        recession_months=rec,
    )
    r = get(rows, "brecha", "precio_industria_vs_deflactor")
    assert r.level == "directo" and r.value < 0  # precio por debajo del deflactor
    assert "bls:PCU623110623110" in r.source_ref and "DEF" in r.source_ref
    # el fenómeno es 6231 y el PPI es de 623110 (subindustria): indirecto, con el nivel usado
    rows = build(
        phen((("6231", 1.0),)), [ev("A")], market({}),
        series=lambda s, i: (dates, ppi), deflator=(dates, defl), deflator_id="DEF",
        prices=prices, recession_months=rec,
    )  # fmt: skip
    r = get(rows, "brecha", "precio_industria_vs_deflactor")
    assert (r.level, r.naics_used) == ("indirecto", "623110")


def test_level_vs_peak_before_last_recession():
    dates, vals = monthly(2017, 100, lambda i: 100 + (i if i < 36 else 36 - (i - 36) * 0.5))
    rec = [(2020 * 12 + 1, 1.0)]  # empieza en 2020-02 (i = 37)
    got = dm.level_vs_peak(dates, vals, rec, TP)
    assert got is not None and got[1] < 0  # sigue bajo el pico previo
    rows = build(phen(), [ev("A")], market({}), series=lambda s, i: (dates, vals),
                 recession_months=rec)  # fmt: skip
    assert get(rows, "brecha", "nivel_vs_pico_previo:bls:A").value == pytest.approx(got[1])


def test_demand_and_evidence_from_phenomenon_series():
    rows = build(phen(), [ev("V", "valor", real_effect=0.7), ev("C", "cantidad")], market({}))
    assert get(rows, "demanda", "crecimiento_real:bls:V").value == 0.7
    assert get(rows, "demanda", "crecimiento_cantidad:bls:C").value == 1.5
    assert get(rows, "evidencia", "q:serie:bls:C").value == 0.02
    assert get(rows, "demanda", "ingresos_industria").level == "sin_dato"


def test_risk_terms_item_1a_with_naics_concordance():
    t = [
        {"object_id": "Item 1A|staffing shortage", "source": "edgar", "q_value": 0.01,
         "naics": [("623", 0.8)], "window_start": None, "window_end": None},
        {"object_id": "Item 7|staffing shortage", "source": "edgar", "q_value": 0.001,
         "naics": [("6231", 1.0)]},
        {"object_id": "Item 1A|chips", "source": "edgar", "q_value": 0.01,
         "naics": [("3344", 1.0)]},
    ]  # fmt: skip
    rows = build(phen(), [ev("A")], market({}), terms=t)
    got = [r for r in rows if r.dimension == "riesgo"]
    assert [r.metric for r in got] == ["termino:staffing shortage"]
    assert (got[0].level, got[0].naics_used) == ("indirecto", "623")


def test_copyrighted_series_are_hidden_but_counted():
    rows = build(phen(), [ev("SECRET1", public=False)], market({}))
    assert all("SECRET1" not in (r.metric + (r.source_ref or "")) for r in rows)
    assert any(r.dimension == "evidencia" and not r.public for r in rows)


def test_table_schema_validates_and_rejects_invalid():
    rows = build(phen(), [ev("A")], market({("6231", "firms", 2022): 1}))
    table = dm.to_table_rows("r1", "pX", rows)
    import pyarrow as pa

    validate(
        pa.Table.from_pylist(table, schema=PHENOMENON_DIMENSIONS.schema), PHENOMENON_DIMENSIONS
    )
    bad = [{**table[0], "level": "inventado"}]
    with pytest.raises(SchemaError):
        validate(
            pa.Table.from_pylist(bad, schema=PHENOMENON_DIMENSIONS.schema), PHENOMENON_DIMENSIONS
        )
    ghost = [{**table[0], "level": "sin_dato", "value": 1.0}]
    with pytest.raises(SchemaError):
        validate(
            pa.Table.from_pylist(ghost, schema=PHENOMENON_DIMENSIONS.schema), PHENOMENON_DIMENSIONS
        )


def test_render_section_hides_nothing_public_and_shows_marks():
    from src.report.render import _dimensions

    html = _dimensions({"dimensions": [
        {"dimension": "competencia", "metric": "empresas", "value": 10663.0, "value_text": None,
         "unit": "u", "period": "2022", "source_ref": "market/naics_metrics:firms",
         "level": "directo", "naics_used": "6231"},
        {"dimension": "competencia", "metric": "cr8", "value": None, "value_text": None,
         "unit": None, "period": None, "source_ref": None, "level": "sin_dato",
         "naics_used": "6231"},
    ]})  # fmt: skip
    assert "Antecedentes (9 dimensiones)" in html and "✅" in html and "⚪" in html
    assert _dimensions({"dimensions": []}) == ""


def test_config_keys_present():
    p = dm.params_from(load_config())
    assert p.size_cr == (4, 8, 20, 50) and p.risk_max_items >= 1
    assert Path(REPO_ROOT / "config.yaml").exists()
