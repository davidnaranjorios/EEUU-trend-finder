"""ADR 0021: el detector de texto no toma artefactos de formato/denominador por tendencias.

- una serie constante (censurada en el piso) no genera señal aunque haya ruido de punto flotante;
- la deriva se corrige por sección: el denominador de una sección no contamina a las otras;
- un salto de plantilla en un solo periodo no es señal; un término que crece de verdad sí;
- sin quiebre estimable (persistencia == recent) la bandera de artefacto no se levanta.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from src.detect.text import AR_BURN_IN, TermTestParams, break_year_histogram, term_test
from src.detect.text_filters import drift_adjust, drift_adjust_by_group
from src.stats.bootstrap import stationary_indices
from src.stats.fdr import TailConfig


def params(recent: int = 6, persistence: int = 4) -> TermTestParams:
    return TermTestParams(
        window=8,
        persistence=persistence,
        recent=recent,
        phi_margin=5.0,
        tail=TailConfig(method="none"),
    )


def idx(rng, b: int = 499, n: int = 60) -> np.ndarray:
    """Índices de stationary bootstrap sobre la línea base (g - recent), como en la corrida."""
    base_len = n - 6
    return stationary_indices(
        base_len, b, 3.0, (int(rng.integers(1 << 30)), 1, base_len), n_out=8 + 6 + AR_BURN_IN
    )


def test_constant_series_with_float_noise_is_not_a_signal():
    rng = np.random.default_rng(1)
    x = np.full(40, -6.0)
    x[-6:] += 2.2e-16  # ruido de punto flotante tras la resta de la deriva
    _, p, _, _ = term_test(x, idx(rng, n=40), params())
    assert p == 1.0


def test_drift_by_section_does_not_leak_between_sections():
    g, r = 30, 6
    base = slice(0, g - r)
    # sección "a": términos constantes cuyo denominador cae al final (deriva real -1);
    # sección "b": términos constantes con denominador estable.
    a = np.full((40, g), -5.0)
    a[:, -r:] -= 1.0
    b = np.full((40, g), -5.0)
    x = np.vstack([a, b])
    mixed, _ = drift_adjust(x, base, 2, 10)
    by_sec, _ = drift_adjust_by_group(x, base, 2, 10, ["a"] * 40 + ["b"] * 40)
    # mezclando secciones, la mediana de "a" desplaza a "b" (efecto espurio);
    # por sección, "b" queda intacto y "a" queda plano tras corregir su propia deriva.
    assert np.ptp(mixed[40:, -r:] - mixed[40:, :-r].mean(axis=1, keepdims=True)) >= 0
    assert np.allclose(by_sec[40:], b)
    assert np.allclose(by_sec[:40], a - (-1.0) * (np.arange(g) >= g - r))


def test_template_jump_in_one_period_is_not_a_signal_but_growth_is():
    rng = np.random.default_rng(3)
    g = 48
    noise = lambda: rng.normal(0, 0.15, g)  # noqa: E731
    jump = -5 + noise()
    jump[-4] += 3.0  # salto de plantilla en un solo periodo
    growth = -5 + noise()
    growth[-6:] += np.linspace(1.0, 3.0, 6)  # crecimiento sostenido
    ix = idx(rng, n=g)
    pj = term_test(jump, ix, params())[1]
    pg = term_test(growth, ix, params())[1]
    assert pj > 0.05
    assert pg < 0.05


def test_break_flag_not_raised_when_break_not_estimable():
    sigs = [{"window_start": date(2024, 10, 1)} for _ in range(20)]
    assert break_year_histogram(sigs, estimable=True)["artifact_suspect"] is True
    h = break_year_histogram(sigs, estimable=False)
    assert h["artifact_suspect"] is False and h["break_estimable"] is False
