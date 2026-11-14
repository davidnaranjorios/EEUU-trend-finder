"""Trayectoria, marcas que no filtran e inflación interpolada (ADR 0025)."""

from __future__ import annotations

import numpy as np

from src.rank import inflation_effect as ix
from src.rank import marks
from src.rank import trajectory as tj
from src.rank.phenomena import direction_of
from src.rank.triangulate import Evidence

P = tj.TrajectoryParams(prior_years=5, recent_years=3, peak_years=2, max_back_years=10)
N = 12 * 14  # 14 años mensuales; la recesión empieza en el mes 96 (a 6 años del final)
START = 96


FIRST = 2000 * 12


def gs(rates: list[tuple[int, float]]) -> np.ndarray:
    """Niveles mensuales con crecimiento por tramos: [(meses, tasa anual compuesta)]."""
    g = np.concatenate([np.full(n, np.log1p(r) / 12) for n, r in rates])
    return 100 * np.exp(np.concatenate([[0.0], np.cumsum(g)]))


def starts_for(_levels: np.ndarray) -> list[int]:
    return [FIRST + START]


def classify(levels, starts, p=P):
    return tj.classify(levels, FIRST, "M", starts, p)


def test_acelera():
    g = gs([(START, 0.02), (N - START, 0.08)])
    assert classify(g, starts_for(g))[0] == "acelera"


def test_gira_al_alza():
    g = gs([(START, -0.03), (N - START, 0.04)])
    assert classify(g, starts_for(g))[0] == "gira_al_alza"


def test_frena_caida_no_cuenta_como_sube():
    g = gs([(START, -0.06), (N - START, -0.02)])
    assert classify(g, starts_for(g))[0] == "frena_caida"
    ev = Evidence("series", "fred", "x", frozenset({"a"}), 0.01, 2.0, ("slope",), True)
    ev.trajectory = "frena_caida"
    assert direction_of([ev], 0.6) == "mixto"
    ev.trajectory = "acelera"
    assert direction_of([ev], 0.6) == "sube"


def test_caida_que_se_frena_con_cola_positiva_sigue_siendo_frena_caida():
    # cae 6 %/año, luego cae más despacio y repunta algo al final: nunca vuelve a su nivel previo
    g = gs([(START, -0.06), (N - START - 36, -0.04), (36, 0.01)])
    assert classify(g, starts_for(g))[0] == "frena_caida"


def test_rebote_bajo_pico():
    # crece, cae un 25 % en un año y se recupera sin volver al pico
    g = gs([(START, 0.03), (12, -0.25), (N - START - 12, 0.05)])
    assert classify(g, starts_for(g))[0] == "rebote_bajo_pico"


def test_sin_recesion_usa_los_ultimos_anios():
    g = gs([(N - 36, 0.01), (36, 0.09)])
    assert classify(g, [])[0] == "acelera"


def test_recession_starts_from_indicator():
    months = [(m, 1.0 if 10 <= m < 14 or 30 <= m < 33 else 0.0) for m in range(0, 40)]
    assert tj.recession_starts(months, "M") == [10, 30]
    assert tj.recession_starts(months, "Q") == [10 // 3, 30 // 3]


def test_phenomenon_label_is_the_most_frequent():
    assert tj.of_phenomenon(["acelera", None, "frena_caida", "acelera"]) == "acelera"
    assert tj.of_phenomenon([None]) is None


def test_isolated_missing_inflation_month_is_interpolated():
    lv = {m: 100.0 * 1.002**m for m in range(0, 24)}
    full = dict(lv)
    del lv[10]
    assert 3 not in ix.inflation_by_grid(lv, "Q")  # el trimestre del mes 10 se pierde
    filled, est = ix.fill_gaps(lv, 1)
    assert est == {10} and abs(filled[10] - full[10]) < 1e-9
    assert 3 in ix.inflation_by_grid(filled, "Q")
    # un hueco más largo que el máximo no se rellena
    del lv[11]
    assert ix.fill_gaps(lv, 1) == (lv, set())
    assert ix.fill_gaps(lv, 2)[1] == {10, 11}


def _ev(kind, **kw):
    base = dict(
        kind="series",
        source="fred",
        object_id="x",
        agencies=frozenset({"a"}),
        q_value=0.01,
        effect_size=1.0,
        detectors=("slope",),
        public=True,
        measure_kind=kind,
    )
    base.update(kw)
    return Evidence(**base)


def test_sin_evidencia_demanda_mark():
    assert marks.sin_evidencia_demanda([_ev("precio")]) is True
    assert marks.sin_evidencia_demanda([_ev("cantidad", measure_subtype="poblacion")]) is True
    assert marks.sin_evidencia_demanda([_ev("cantidad")]) is False
    assert marks.sin_evidencia_demanda([_ev("valor", real_effect=None)]) is True
    assert marks.sin_evidencia_demanda([_ev("valor", real_effect=-0.5)]) is True
    assert marks.sin_evidencia_demanda([_ev("valor", real_effect=0.5)]) is False
    assert marks.sin_evidencia_demanda([]) is None


def test_origen_posiblemente_compartido_mark():
    a = _ev("precio", agencies=frozenset({"BLS"}), chain=("BLS (IPC)",))
    b = _ev("precio", agencies=frozenset({"Fed"}), chain=("BLS (IPC)", "Fed"))
    assert marks.origen_posiblemente_compartido([a, b]) is True
    c = _ev("precio", agencies=frozenset({"Fed"}), chain=("otro",))
    assert marks.origen_posiblemente_compartido([a, c]) is False
    assert marks.origen_posiblemente_compartido([a, _ev("precio", possible_duplicates=True)])
