"""Stationary bootstrap con índices compartidos por familia y longitud de bloque automática.

- Stationary bootstrap: Politis & Romano (1994). Cada réplica es una concatenación de bloques
  con inicio uniforme y longitud geométrica de media `L`, envolviendo al final de la muestra.
- Longitud automática: Politis & White (2004) con la corrección de Patton, Politis & White
  (2009), la misma regla que usa la librería `arch`.
- Dependencia entre series: se generan **unos mismos índices** de tiempo para toda la familia
  (CLAUDE.md, "Reglas estadísticas"); las series se alinean por su fin (posición relativa al
  último dato), así que una réplica toma los mismos periodos de todas.
- Los índices salen de una semilla y de la forma del problema (`seed_key`), de modo que dos
  procesos o dos shards que trabajan la misma familia regeneran exactamente los mismos.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def stationary_indices(
    n: int,
    n_resamples: int,
    block_length: float,
    seed_key: Sequence[int],
    n_out: int | None = None,
) -> np.ndarray:
    """Matriz (B, n_out) de índices en 0..n-1 de un stationary bootstrap con bloque medio `L`.

    `n_out` (por defecto `n`) permite construir réplicas de otro largo a partir de una muestra
    base de `n` observaciones.
    """
    if n < 2:
        raise ValueError("se necesitan al menos 2 observaciones")
    m = n if n_out is None else n_out
    rng = np.random.default_rng(list(seed_key))
    p = 1.0 / max(float(block_length), 1.0)
    idx = np.empty((n_resamples, m), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, n_resamples)
    new_block = rng.random((n_resamples, m)) < p
    starts = rng.integers(0, n, (n_resamples, m))
    for t in range(1, m):
        idx[:, t] = np.where(new_block[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return idx


def optimal_block_length(x: np.ndarray) -> float:
    """Longitud media óptima del stationary bootstrap (Politis-White 2004, Patton et al. 2009).

    Devuelve al menos 1. `x` sin NaN.
    """
    x = np.asarray(x, dtype=float)
    n = x.size
    if n < 8 or not np.isfinite(x).all():
        return 1.0
    xc = x - x.mean()
    var = xc @ xc / n
    if var <= 0:
        return 1.0
    kn = max(5, int(math.ceil(math.sqrt(math.log10(n)))))
    m_max = int(math.ceil(math.sqrt(n))) + kn
    b_max = math.ceil(min(3 * math.sqrt(n), n / 3))
    c = 2.0
    lags = min(m_max + kn, n - 1)
    acov = np.array([xc[: n - k] @ xc[k:] / n for k in range(lags + 1)])
    rho = acov / acov[0]
    threshold = c * math.sqrt(math.log10(n) / n)
    # Primer m tal que las kn autocorrelaciones siguientes son todas insignificantes.
    m_hat = None
    for m in range(1, lags - kn + 2):
        if np.all(np.abs(rho[m : m + kn]) < threshold):
            m_hat = m
            break
    if m_hat is None:
        sig = np.nonzero(np.abs(rho[1:]) > threshold)[0]
        m_hat = int(sig[-1]) + 1 if sig.size else 1
    big_m = min(2 * m_hat, m_max, lags)
    if big_m < 1:
        return 1.0
    k = np.arange(-big_m, big_m + 1)
    t = np.abs(k) / big_m
    lam = np.where(t <= 0.5, 1.0, 2.0 * (1.0 - t))
    lam[t > 1] = 0.0
    r = acov[np.abs(k)]
    g_hat = float(np.sum(lam * np.abs(k) * r))
    g0 = float(np.sum(lam * r))
    d_sb = 2.0 * g0**2
    if d_sb <= 0 or g_hat == 0:
        return 1.0
    b = (2.0 * g_hat**2 / d_sb) ** (1.0 / 3.0) * n ** (1.0 / 3.0)
    return float(min(max(b, 1.0), b_max))


def family_block_length(series: Sequence[np.ndarray], setting: int | str) -> float:
    """Longitud de bloque de una familia: el entero configurado o la mediana de las óptimas."""
    if setting != "auto":
        return float(setting)
    if not series:
        return 1.0
    return float(np.median([optimal_block_length(s) for s in series]))


# --- Post-blackening (Davison & Hinkley 1997, §8.2.4) -----------------------------------------
#
# Con crecimiento muy persistente (AR(1) φ≈0,7) y 10–15 años de datos, remuestrear bloques de la
# serie observada subestima la cola del nulo: la muestra no contiene toda su variación de baja
# frecuencia (medido: q99 0,99 frente a 1,17 real). Se ajusta un AR(1), se remuestrean **sus
# residuos** con el stationary bootstrap (los mismos índices para toda la familia) y se
# re-colorea con el AR. El bloque recoge la dependencia que el AR(1) no explica.
#
# φ se estima desde las diferencias (robusto a un cambio de media), con la corrección de sesgo
# de Kendall (+(1+3φ)/n) y un margen conservador de `margin` errores estándar
# (sqrt((1-φ²)/n)). Tamaño medido del detector slope sobre 1.500 series nulas (W=60, 15 años):
#   Yule-Walker AR(p<=4) sin margen: 7,5 % y 2,0 % a niveles 5 % y 1 % con AR(1) φ=0,7;
#   diferencias + Kendall:            5,8 % y 1,5 %;
#   ... + margen 1 se (elegido):      3,8 % y 0,9 % (iid: 3,7 % y 0,5 %).

AR_BURN_IN = 60
PHI_MAX = 0.98


def fit_ar1(x: np.ndarray, margin: float = 1.0) -> np.ndarray:
    """φ conservador de un AR(1) para el post-blackening (ver arriba).

    Se estima desde las diferencias, corr(Δx_t, Δx_{t-1}) = -(1-φ)/2: un cambio de media en
    `x` solo toca una diferencia, así que no se confunde con persistencia (medido: con un salto
    de 3 sd en ruido iid, Yule-Walker da φ=0,67 y este estimador 0,03).
    """
    d = np.diff(x)
    d = d - d.mean()
    den = d @ d
    n = x.size
    if d.size < 3 or den <= 0:
        return np.zeros(0)
    phi = 1.0 + 2.0 * (d[1:] @ d[:-1]) / den
    phi += (1 + 3 * phi) / n + margin * np.sqrt(max(1 - phi**2, 0.01) / n)
    return np.array([float(np.clip(phi, -PHI_MAX, PHI_MAX))])


def prewhiten(x: np.ndarray, phi: np.ndarray) -> np.ndarray:
    """Residuos e_t = x_t - sum φ_i x_{t-i} (con x = 0 antes del inicio), centrados; largo n."""
    xc = x - x.mean()
    e = xc.copy()
    for i, c in enumerate(phi, start=1):
        e[i:] -= c * xc[:-i]
    return e - e.mean()


def recolor(e: np.ndarray, phi: np.ndarray, burn_in: int = AR_BURN_IN) -> np.ndarray:
    """Aplica el AR a innovaciones remuestreadas `e` (..., T) y descarta `burn_in` periodos."""
    if phi.size == 0:
        return e[..., burn_in:]
    x = np.zeros_like(e)
    for t in range(e.shape[-1]):
        acc = e[..., t].copy()
        for i, c in enumerate(phi, start=1):
            if t - i >= 0:
                acc += c * x[..., t - i]
        x[..., t] = acc
    return x[..., burn_in:]


def ar_null(sample: np.ndarray, idx: np.ndarray, phi: np.ndarray) -> np.ndarray:
    """Réplicas nulas (B, T - burn_in) de `sample` por post-blackening con índices `idx` (B, T)."""
    return recolor(prewhiten(sample, phi)[idx], phi)
