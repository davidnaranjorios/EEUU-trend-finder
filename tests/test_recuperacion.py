"""Recuperación post-choque (ADR 0034): pico previo o tendencia pre-choque; clase del fenómeno."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import numpy as np

from src.rank import change as ch
from src.rank import trajectory as tj
from src.rank.triangulate import Evidence, assess

DATA = Path(__file__).parent / "data"  # series reales de FRED/BLS (dominio público)


def _load(name: str) -> list[tuple[date, float]]:
    with (DATA / f"{name}.csv").open() as f:
        return [(date.fromisoformat(r["date"]), float(r["value"])) for r in csv.DictReader(f)]


def _classify(name: str = "CES6562310001"):
    rows = _load(name)
    first = rows[0][0].year * 12 + rows[0][0].month - 1
    levels = np.array([v for _, v in rows])
    months = [(d.year * 12 + d.month - 1, v) for d, v in _load("USREC")]
    starts = tj.recession_starts(months, "M")
    return tj.classify(levels, first, "M", starts, tj.TrajectoryParams())


def test_real_employment_6231_is_rebote_bajo_pico_and_recovery():
    label, detail = _classify()
    assert label == "rebote_bajo_pico"
    assert detail["recuperacion"] is True
    assert detail["bajo_pico"] is True


def test_real_employment_pre_shock_trend_is_evaluated_and_not_the_reason():
    """La tendencia 2015-2019 (el ancla sale de USREC) del empleo 6231 era plana o a la baja: hoy
    está sobre ella. Lo que lo hace recuperación es el pico previo, no la tendencia."""
    _, detail = _classify()
    assert detail["bajo_tendencia"] is False and detail["bajo_pico"] is True


def test_preshock_trend_detects_recovery_even_above_the_old_peak():
    """Pico previo bajo (serie plana) pero tendencia pre-choque fuerte: sigue bajo la tendencia."""
    n = 12 * 14
    start = 96
    g = np.concatenate([np.full(start, np.log1p(0.05) / 12), np.full(n - start, 0.0)])
    levels = 100 * np.exp(np.concatenate([[0.0], np.cumsum(g)]))
    first = 2000 * 12
    p = tj.TrajectoryParams()
    _, d = tj.classify(levels, first, "M", [first + start], p)
    assert d["bajo_tendencia"] is True and d["recuperacion"] is True
    # Con la tolerancia enorme deja de contar como «bajo la tendencia».
    p2 = tj.TrajectoryParams(preshock_tolerance_pct=60.0)
    _, d2 = tj.classify(levels, first, "M", [first + start], p2)
    assert d2["bajo_tendencia"] is False


def test_no_recession_in_range_skips_the_trend_test():
    levels = 100 * np.exp(np.arange(120) * 0.003)
    _, d = tj.classify(levels, 2000 * 12, "M", [], tj.TrajectoryParams())
    assert d["bajo_tendencia"] is None and d["recuperacion"] is False


def _ev(sid, kind="cantidad", rec=True, sub=None):
    return Evidence(
        "series", "bls", sid, frozenset({"bls"}), 1e-6, 1.0, ("slope",), True,
        measure_kind=kind, measure_subtype=sub, recovery=rec,
    )  # fmt: skip


def test_phenomenon_is_recovery_only_if_all_quantity_series_are():
    assert tj.class_of([("cantidad", None, True), ("cantidad", None, True)]) == "recuperacion"
    assert tj.class_of([("cantidad", None, True), ("cantidad", None, False)]) == "estructural"
    assert tj.class_of([("cantidad", None, True), ("cantidad", None, None)]) == "estructural"
    # Población y valor no deciden; sin cantidad evaluable no se puede decir «recuperación».
    assert (
        tj.class_of([("cantidad", "poblacion", False), ("cantidad", None, True)]) == "recuperacion"
    )
    assert tj.class_of([("valor", None, True)]) == "estructural"


def test_recovery_candidate_leaves_the_main_ranking_but_keeps_status():
    c = assess(__import__("src.rank.triangulate", fromlist=["Candidate"]).Candidate(
        "c", "series", [_ev("A"), Evidence("series", "census", "B", frozenset({"census"}), 1e-6,
        1.0, ("slope",), True, measure_kind="valor")], 2), 2)  # fmt: skip
    assert (c.status, c.clase, c.published) == ("candidato", "recuperacion", False)


def test_recovery_row_in_change_uses_all_series_including_employment():
    """CES6562310001 (insumo, rebote_bajo_pico) → recuperación = sí en el fenómeno de 6231."""
    label, detail = _classify()
    emp = {
        "item_kind": "series", "source": "fred", "object_id": "CES6562310001",
        "measure_kind": "cantidad", "measure_subtype": None, "trajectory": label,
        "recuperacion": detail["recuperacion"], "public": True, "q_value": 0.02,
    }  # fmt: skip
    from tests.test_change import ctx

    qty, rec = ch.classify_quantity([emp], ctx({("fred", "CES6562310001"): "insumo"}))
    assert rec.status == "si" and rec.value == 1.0
    assert qty.status == "sin_dato"  # el empleo sigue sin contar como cantidad producida
