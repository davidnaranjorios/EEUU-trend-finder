"""Criterio de terminado de la Fase 2 (CLAUDE.md) sobre datos sintéticos.

1. Ruido puro (iid), AR(1) y series correlacionadas entre sí: la FDR queda dentro de lo
   esperado por q, en familias con tendencias inyectadas y bajo el nulo global.
2. Una tendencia inyectada se detecta.
3. Un salto aislado no genera señal (persistencia).
4. El reporte de la corrida trae el conteo BH y el BY de comparación (test_detect_run.py).

Además: la cola GPD coincide con el p empírico de muchas más réplicas (decisión A de David).
"""

from __future__ import annotations

import math
import zlib

import numpy as np
import pytest

from src.detect.family import FamilyParams, Item, run_family
from src.detect.numeric import cusum_stat
from src.stats.bootstrap import optimal_block_length, stationary_indices
from src.stats.fdr import TailConfig, bh_adjust, bootstrap_p, by_adjust, empirical_p
from src.stats.transform import prepare

Q = 0.05
N_GROWTH = 180  # 15 años mensuales: el lookback de regime en producción
WINDOW = 60  # ventana de slope en producción (5 + 5 años)
PERSIST = 6  # persistence.min_consecutive_periods.M
# Mismas réplicas que en producción (999): con menos, la cola GPD no se activa y el piso del p
# impide descubrir nada. 4 procesos, como el runner de GitHub Actions.
PARAMS = FamilyParams(n_resamples=999, seed=7, window=WINDOW, workers=4)


def noise(kind: str, n_series: int, n: int, rng: np.random.Generator) -> np.ndarray:
    """Crecimiento con media 0 y sd marginal 1: iid, AR(1) φ=0,7 o un factor común (ρ≈0,5)."""
    if kind == "iid":
        return rng.standard_normal((n_series, n))
    if kind == "ar1":
        return _ar1(rng.standard_normal((n_series, n)), 0.7)
    if kind == "factor":
        common = _ar1(rng.standard_normal((1, n)), 0.5)
        own = _ar1(rng.standard_normal((n_series, n)), 0.5)
        return math.sqrt(0.5) * common + math.sqrt(0.5) * own
    raise ValueError(kind)


# sd de largo plazo del crecimiento de cada tipo de ruido: la tendencia inyectada se mide en
# esas unidades, porque es lo que determina cuán distinguible es de la deriva del ruido.
LONG_RUN_SD = {"iid": 1.0, "ar1": math.sqrt(1.7 / 0.3), "factor": math.sqrt(3.0)}


def _ar1(e: np.ndarray, phi: float) -> np.ndarray:
    x = np.empty_like(e)
    x[:, 0] = e[:, 0]
    for t in range(1, e.shape[1]):
        x[:, t] = phi * x[:, t - 1] + math.sqrt(1 - phi**2) * e[:, t]
    return x


def items_from_growth(growth: np.ndarray, n_growth: int) -> list[Item]:
    """Pasa cada serie por la misma preparación que los datos reales (winsorización incl.)."""
    out = []
    for g in growth:
        level = np.concatenate([[0.0], np.cumsum(g)])  # incluye 0: no se usa log
        prep = prepare(
            level, 0, n_growth, "M", seasonal_adjust=False, winsor_mad=4.0, max_gap_share=0.1
        )
        assert prep is not None
        out.append(Item(prep.growth, prep.scale, PERSIST, "M"))
    return out


def discoveries(detector: str, growth: np.ndarray, params: FamilyParams = PARAMS) -> np.ndarray:
    items = items_from_growth(growth, N_GROWTH)
    results, _ = run_family(detector, items, params)
    p = np.array([r.p_value for r in results])
    return bh_adjust(p) <= Q


def inject_trend(growth: np.ndarray, rows: slice, size: float, last: int) -> np.ndarray:
    out = growth.copy()
    out[rows, -last:] += size
    return out


# --- 1. FDR dentro de q ------------------------------------------------------------------------

N_SERIES = 60
N_SIGNAL = 6
REPS = 12


@pytest.mark.parametrize("detector", ["slope", "regime"])
@pytest.mark.parametrize("kind", ["iid", "ar1", "factor"])
def test_fdr_within_q_with_injected_trends(detector, kind):
    rng = np.random.default_rng(zlib.crc32(f"{detector}-{kind}".encode()))
    fdp, power = [], []
    for _ in range(REPS):
        g = inject_trend(
            noise(kind, N_SERIES, N_GROWTH, rng),
            slice(0, N_SIGNAL),
            2.5 * LONG_RUN_SD[kind],
            WINDOW,
        )
        found = discoveries(detector, g)
        false = int(found[N_SIGNAL:].sum())
        fdp.append(false / max(int(found.sum()), 1))
        power.append(found[:N_SIGNAL].mean())
    mean_fdp = float(np.mean(fdp))
    se = float(np.std(fdp, ddof=1) / math.sqrt(REPS)) if REPS > 1 else 0.0
    # BH controla la FDR en q·m0/m = 0,045; margen de 2 errores estándar de la media.
    assert mean_fdp <= Q + 2 * se + 1e-9, f"FDR {mean_fdp:.3f} (se {se:.3f})"
    assert np.mean(power) > 0.5, f"potencia {np.mean(power):.2f}"


@pytest.mark.parametrize("detector", ["slope", "regime"])
@pytest.mark.parametrize("kind", ["iid", "ar1", "factor"])
def test_global_null_rarely_discovers(detector, kind):
    """Bajo el nulo global, FDR = P(alguna señal): debe quedar cerca de q o por debajo."""
    rng = np.random.default_rng(zlib.crc32(f"null-{detector}-{kind}".encode()))
    reps = 25
    any_found = [discoveries(detector, noise(kind, 40, N_GROWTH, rng)).any() for _ in range(reps)]
    rate = float(np.mean(any_found))
    assert rate <= Q + 2 * math.sqrt(Q * (1 - Q) / reps), f"{rate:.3f} de réplicas con señal"


# --- 2. Tendencia inyectada --------------------------------------------------------------------


@pytest.mark.parametrize("detector", ["slope", "regime"])
@pytest.mark.parametrize("kind", ["iid", "ar1", "factor"])
def test_injected_trend_is_detected(detector, kind):
    """Un cambio sostenido de 3 sd de largo plazo en los últimos 3 años, entre 49 series nulas."""
    rng = np.random.default_rng(zlib.crc32(f"trend-{detector}-{kind}".encode()))
    g = inject_trend(noise(kind, 50, N_GROWTH, rng), slice(0, 1), 3.0 * LONG_RUN_SD[kind], WINDOW)
    found = discoveries(detector, g)
    assert found[0], "la tendencia inyectada no se detectó"


# --- 3. Salto aislado ---------------------------------------------------------------------------


@pytest.mark.parametrize("detector", ["slope", "regime"])
@pytest.mark.parametrize("where", [10, WINDOW // 2, WINDOW - 2])
def test_isolated_jump_gives_no_signal(detector, where):
    """Un escalón de 25 sd en el nivel (un solo periodo de crecimiento) dentro de la ventana."""
    rng = np.random.default_rng(23 + where)
    g = noise("iid", 50, N_GROWTH, rng)
    g[0, -where] += 25.0
    found = discoveries(detector, g)
    assert not found[0], "un salto aislado generó señal"


def test_persistence_gate_zeroes_a_jump_in_the_statistic():
    from src.detect.numeric import slope_stat

    g = np.zeros(2 * WINDOW)
    g[-5] = 25.0
    t, effect, persistent = slope_stat(g, WINDOW, PERSIST)
    assert not persistent and t == 0.0


# --- Cola GPD frente a más réplicas (opción 2) -------------------------------------------------


def test_gpd_tail_agrees_with_many_more_resamples():
    """En pruebas que quedan en el piso con B=999, la GPD aproxima el p de B=200.000."""
    rng = np.random.default_rng(5)
    n = 120
    checked = 0
    for shift in (0.55, 0.6, 0.65, 0.7):
        g = rng.standard_normal(n)
        g[-48:] += shift
        t_obs, _ = cusum_stat(g, PERSIST)
        gc = g - g.mean()
        small = cusum_stat(gc[stationary_indices(n, 999, 1.0, [1, n])], PERSIST)[0]
        p_small, exceed = empirical_p(float(t_obs), small)
        if exceed >= 10:
            continue  # no está en el piso: la GPD no se usa
        p_gpd, method = bootstrap_p(float(t_obs), small, TailConfig())
        big = np.concatenate(
            [
                cusum_stat(gc[stationary_indices(n, 20_000, 1.0, [2, n, k])], PERSIST)[0]
                for k in range(10)
            ]
        )
        p_big, exceed_big = empirical_p(float(t_obs), big)
        assert method == "gpd"
        if exceed_big >= 10:  # p de referencia confiable
            assert abs(math.log10(p_gpd) - math.log10(p_big)) < 0.5, (p_gpd, p_big)
            checked += 1
    assert checked >= 2, "ningún caso quedó en el rango comparable"


# --- Unidades ----------------------------------------------------------------------------------


def test_bh_and_by_adjust():
    p = np.array([0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216])
    q = bh_adjust(p)
    assert (q <= Q).sum() == 2
    assert np.all(by_adjust(p) >= q)
    assert np.all(np.diff(q[np.argsort(p)]) >= 0)


def test_shared_indices_are_reproducible_and_shared():
    a = stationary_indices(50, 99, 4.0, [1, 2, 3])
    b = stationary_indices(50, 99, 4.0, [1, 2, 3])
    assert np.array_equal(a, b)
    assert a.min() >= 0 and a.max() < 50
    # Bloques: la mayoría de los pasos avanza de a uno (longitud media 4 → p de corte 1/4).
    adv = np.mean((a[:, 1:] - a[:, :-1]) % 50 == 1)
    assert 0.65 < adv < 0.85


def test_block_length_grows_with_autocorrelation():
    rng = np.random.default_rng(3)
    iid = rng.standard_normal(500)
    ar = _ar1(rng.standard_normal((1, 500)), 0.8)[0]
    assert optimal_block_length(ar) > optimal_block_length(iid)
