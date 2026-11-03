"""Detectores numéricos sobre crecimiento preparado, con p calibrado por stationary bootstrap.

Dos detectores, cada uno una familia FDR por corrida:

- `slope`: cambio de pendiente. Theil-Sen del log-nivel en la ventana reciente (W periodos)
  frente a la previa (W periodos). El nulo se remuestrea de la **historia anterior a la
  ventana reciente** (línea base): bajo el nulo tiene la misma distribución, y bajo la
  alternativa no arrastra el cambio que se quiere detectar. La persistencia es parte del
  estadístico: si en la ventana reciente no hay una racha de al menos `k` periodos
  consecutivos con crecimiento del lado del cambio (respecto de la pendiente previa), T = 0.
  Se aplica igual a cada réplica, así que el p-value sigue siendo válido y un salto aislado
  (un solo periodo) no cuenta.
- `regime`: quiebre de régimen en la media del crecimiento. CUSUM normalizado
  max_t |S_t| / (sd · sqrt(N)) con t restringido a [k, N-k] (segmentos de al menos `k`
  periodos). PELT (`ruptures`) no prueba nada: solo fecha el último quiebre de las series que
  sobreviven a BH (ver `locate_break`), porque correrlo en cada réplica cuesta ~10⁴ veces más
  (medido en docs/decisions/0005-detector-numerico.md).

El nulo de ambos es "crecimiento estacionario con media constante". Las réplicas salen de un
stationary bootstrap con post-blackening (`src.stats.bootstrap.ar_null`) sobre la línea base en
`slope` y la serie completa en `regime`, con los mismos índices para toda la subfamilia (mismo
largo y grilla), lo que conserva la autocorrelación y la correlación entre series.

Descartado tras medirlo (tests/test_detect_fdr.py): remuestrear residuos del modelo con cambio
(centrar cada tramo, Hall & Wilson 1991) o estimar la longitud de bloque sobre ellos. Ganan
potencia, pero con AR(1) φ=0,7 la FDR medida subió a 0,12–0,52, porque centrar tramos cortos
quita variación de baja frecuencia y acorta los bloques.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import ruptures as rpt

from src.stats.bootstrap import ar_null, fit_ar1
from src.stats.fdr import TailConfig, bootstrap_p

DETECTORS = ("slope", "regime")


@dataclass
class DetectorResult:
    statistic: float
    effect: float  # estandarizado por la sd robusta del crecimiento
    persistent: bool
    p_value: float
    p_method: str
    break_pos: int  # posición (en el crecimiento) donde empieza la ventana o el régimen


# --- slope: Theil-Sen con persistencia --------------------------------------------------------


def _theil_sen_from_growth(g: np.ndarray) -> np.ndarray:
    """Pendiente Theil-Sen del nivel acumulado de `g` (..., w): mediana de las pendientes."""
    w = g.shape[-1]
    lv = np.concatenate([np.zeros((*g.shape[:-1], 1)), np.cumsum(g, axis=-1)], axis=-1)
    i, j = np.triu_indices(w + 1, 1)
    slopes = (lv[..., j] - lv[..., i]) / (j - i)
    return np.median(slopes, axis=-1)


def _longest_run(mask: np.ndarray) -> np.ndarray:
    """Racha más larga de True en el último eje."""
    run = np.zeros(mask.shape[:-1], dtype=np.int64)
    best = np.zeros_like(run)
    for t in range(mask.shape[-1]):
        run = np.where(mask[..., t], run + 1, 0)
        best = np.maximum(best, run)
    return best


def slope_stat(g: np.ndarray, window: int, persistence: int) -> tuple[np.ndarray, ...]:
    """(T, efecto, persistente) para crecimientos `g` (..., 2·window)."""
    prior, recent = g[..., :window], g[..., window:]
    b_prior = _theil_sen_from_growth(prior)
    b_recent = _theil_sen_from_growth(recent)
    effect = b_recent - b_prior
    side = np.sign(effect)[..., None]
    mask = side * (recent - b_prior[..., None]) > 0
    persistent = _longest_run(mask) >= persistence
    return np.where(persistent, np.abs(effect), 0.0), effect, persistent


def null_sample(detector: str, g: np.ndarray, window: int) -> np.ndarray:
    """Muestra que se remuestrea bajo el nulo: línea base en `slope`, todo en `regime`."""
    return g[:-window] if detector == "slope" else g


def slope_test(
    g: np.ndarray,
    scale: float,
    idx: np.ndarray,
    window: int,
    persistence: int,
    tail: TailConfig,
    phi_margin: float,
) -> DetectorResult:
    """Prueba de cambio de pendiente.

    `g` es la historia de la serie (al menos 2·W); el estadístico usa los últimos 2·W
    periodos y las réplicas se arman con `idx` (B, 2W + burn-in) sobre la línea base `g[:-W]`.
    """
    if g.size < 2 * window:
        raise ValueError("serie demasiado corta para la ventana")
    t_obs, eff, pers = slope_stat(g[-2 * window :], window, persistence)
    base = null_sample("slope", g, window)
    t_null, _, _ = slope_stat(ar_null(base, idx, fit_ar1(base, phi_margin)), window, persistence)
    if float(t_obs) == 0.0:
        p, method = 1.0, "empirical"
    else:
        p, method = bootstrap_p(float(t_obs), t_null, tail)
    return DetectorResult(
        statistic=float(t_obs),
        effect=float(eff) / scale,
        persistent=bool(pers),
        p_value=p,
        p_method=method,
        break_pos=g.size - window,
    )


# --- regime: CUSUM -----------------------------------------------------------------------------


def cusum_stat(g: np.ndarray, persistence: int) -> tuple[np.ndarray, np.ndarray]:
    """(T, argmax) del CUSUM normalizado de `g` (..., N), con segmentos >= `persistence`."""
    n = g.shape[-1]
    e = g - g.mean(axis=-1, keepdims=True)
    sd = e.std(axis=-1)
    s = np.abs(np.cumsum(e, axis=-1))[..., persistence - 1 : n - persistence]
    sd = np.where(sd > 0, sd, np.inf)
    return s.max(axis=-1) / (sd * np.sqrt(n)), s.argmax(axis=-1) + persistence


def regime_test(
    g: np.ndarray,
    scale: float,
    idx: np.ndarray,
    persistence: int,
    tail: TailConfig,
    phi_margin: float,
) -> DetectorResult:
    """Prueba de quiebre de régimen (CUSUM) con los índices compartidos `idx` (B, N + burn-in)."""
    n = g.shape[-1]
    if n < 2 * persistence + 1:
        raise ValueError("serie demasiado corta para la persistencia exigida")
    t_obs, pos = cusum_stat(g, persistence)
    # φ desde las diferencias: el quiebre que está en la muestra no se confunde con persistencia.
    # (Ajustar sobre la serie centrada por tramos en el argmax subestimaba φ: FDR 0,25 con
    # AR(1) φ=0,7.)
    t_null, _ = cusum_stat(ar_null(g, idx, fit_ar1(g, phi_margin)), persistence)
    p, method = bootstrap_p(float(t_obs), t_null, tail)
    b = int(pos)
    effect = (g[b:].mean() - g[:b].mean()) / scale
    return DetectorResult(
        statistic=float(t_obs),
        effect=float(effect),
        persistent=True,
        p_value=p,
        p_method=method,
        break_pos=b,
    )


def locate_break(
    g: np.ndarray, scale: float, persistence: int, jump: int, penalty: float, fallback: int
) -> tuple[int, float]:
    """Último quiebre según PELT (l2) y el efecto estandarizado del último régimen.

    Si PELT no encuentra quiebres, se usa `fallback` (el argmax del CUSUM).
    """
    n = g.size
    pen = penalty * np.log(n) * scale**2
    bkps = rpt.Pelt(model="l2", min_size=persistence, jump=jump).fit(g).predict(pen=pen)
    inner = [b for b in bkps if b < n]
    b = inner[-1] if inner else fallback
    b = int(min(max(b, persistence), n - persistence))
    return b, float((g[b:].mean() - g[:b].mean()) / scale)
