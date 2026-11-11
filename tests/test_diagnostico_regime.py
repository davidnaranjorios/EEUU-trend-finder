"""Diagnóstico de `regime` (ver docs/decisions/0022-diagnostico-regime.md): 0 BH en 260.772 pruebas.

Mide, con la tubería real, que el detector SÍ ve un quiebre fuerte (no hay falla del CUSUM ni de la
escalación) y que su p mínimo queda pegado a 1/(B_esc+1): con m grande BH no puede descubrir nada
aunque el quiebre sea evidente. No cambia la calibración.
"""

from __future__ import annotations

import numpy as np

from src.config import load_config
from src.detect.family import FamilyParams, Item, run_family
from src.stats.fdr import TailConfig, bh_adjust

ESC = 20_000

# Las funciones del script `eval/diagnostico_regime.py` se repiten aquí a propósito: importar
# `eval` desde un test contamina sys.modules y rompe test_isolation (src nunca carga eval).


def ar1(rng: np.random.Generator, n: int, phi: float) -> np.ndarray:
    e = rng.normal(size=n + 50)
    x = np.zeros(n + 50)
    for t in range(1, n + 50):
        x[t] = phi * x[t - 1] + e[t]
    return x[50:]


def params(cfg: dict, escalate: int) -> FamilyParams:
    d = cfg["detect"]
    cal = cfg["fdr"]["calibration"]
    return FamilyParams(
        n_resamples=cal["n_resamples"],
        block_length=cal["block_length"],
        seed=cal["seed"],
        phi_margin=d["phi_margin"],
        regime_escalate=escalate,
        tail=TailConfig(**d["tail"]),
    )


def bh_count(p_signal: np.ndarray, m: int, q: float = 0.05) -> int:
    p = np.concatenate([p_signal, np.ones(m - p_signal.size)])
    return int((bh_adjust(p) <= q).sum())


def test_strong_break_detected_but_p_is_pinned_to_escalation_floor():
    cfg = load_config()
    rng = np.random.default_rng(5)
    n, k = 60, 4
    items = [Item(ar1(rng, n, 0.3), 1.0, k, "Q") for _ in range(200)]
    for _ in range(8):
        g = ar1(rng, n, 0.3)
        g = g / g.std()
        g[n // 2 :] += 8.0
        items.append(Item(g, 1.0, k, "Q"))
    res, _ = run_family("regime", items, params(cfg, escalate=ESC))
    p = np.array([r.p_value for r in res])
    p_noise, p_break = p[:200], p[200:]
    # 1) el detector ve el quiebre fuerte (CUSUM y escalación funcionan)
    assert np.median(p_break) < 1e-3
    # 2) pero el p no baja mucho de 1/(B_esc+1): el piso lo fija el número de réplicas
    assert p_break.min() >= 0.05 / (ESC + 1)
    # 3) el ruido no produce p diminutos (sin falsos descubrimientos por la escalación)
    assert (p_noise < 1e-3).mean() < 0.02
    # 4) consecuencia: con pocas pruebas BH descubre; con m = 260.772 no (p_min > 0,05/m)
    assert bh_count(p_break, 2_000) >= 1
    assert bh_count(p_break, 260_772) == 0
    assert p_break.min() > 0.05 / 260_772
