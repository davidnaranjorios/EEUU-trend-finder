"""ADR 0023: el efecto de tasas/inflación exige lo mismo que `slope` (W + max(W, línea base)).

Con la exigencia anterior (2·W + max(W, base) = 73 trimestres) ningún cluster trimestral de
15 años (60 periodos) se evaluaba; ahora sí (>= 53) y las anuales de 15 años siguen fuera (< 40).
"""

from __future__ import annotations

import numpy as np

from src.config import load_config
from src.rank import rate_effect as rx

CFG = load_config()
LAST_M = 2024 * 12 + 11


def _levels(rng, n_months: int = 60 * 3 + 80) -> dict[int, float]:
    steps = rng.normal(0, 0.05, n_months)
    first = LAST_M - n_months + 1
    return {first + i: float(v) for i, v in enumerate(2 + np.cumsum(steps))}


def _items(rng, grid: str, n: int, k: int = 6) -> dict:
    last = {"Q": LAST_M // 3, "M": LAST_M, "A": LAST_M // 12}[grid]
    return {f"{grid}{n}-{i}": (rng.normal(0.002, 0.004, n), last, grid, 0.004, 3) for i in range(k)}


def _run(items: dict) -> dict:
    rng = np.random.default_rng(3)
    lv = _levels(rng)
    p = rx.params_from(CFG)
    dr = {g: rx.rate_changes(lv, g) for g in ("M", "Q", "A")}
    levels = {g: rx.rate_levels(lv, g) for g in ("M", "Q", "A")}
    return rx.evaluate(items, dr, p, levels)


def test_required_periods_follow_slope():
    p = rx.params_from(CFG)
    # W + max(W, línea base): M 60+60, Q 20+33, A 7+33
    assert rx.min_growth_periods(p, "M") == (120 + 60, 120)
    assert rx.min_growth_periods(p, "Q") == (73, 53)
    assert rx.min_growth_periods(p, "A") == (47, 40)


def test_quarterly_15y_is_now_evaluated_but_annual_and_short_are_not():
    rng = np.random.default_rng(1)
    res = _run({**_items(rng, "Q", 60), **_items(rng, "Q", 52, 2), **_items(rng, "A", 15, 2)})
    q60 = [r for c, r in res.items() if c.startswith("Q60")]
    assert all(r.evaluated for r in q60)
    for c, r in res.items():
        if c.startswith("Q52") or c.startswith("A15"):
            assert not r.evaluated and r.reason == "serie_corta"


def test_monthly_between_10_and_15_years_is_evaluated():
    rng = np.random.default_rng(2)
    res = _run({**_items(rng, "M", 130), **_items(rng, "M", 110, 2)})
    assert all(r.evaluated for c, r in res.items() if c.startswith("M130"))
    assert all(r.reason == "serie_corta" for c, r in res.items() if c.startswith("M110"))
