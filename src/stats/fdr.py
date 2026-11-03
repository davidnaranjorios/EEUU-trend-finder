"""p-values calibrados con bootstrap (con cola GPD) y corrección FDR (BH decide, BY compara).

Cola GPD: Knijnenburg, Wessels, Reinders & Shmulevich (2009), "Fewer permutations, more
accurate P-values", Bioinformatics 25(12), i161–i168, DOI 10.1093/bioinformatics/btp211.
Cuando el estadístico observado supera a casi todas las réplicas (menos de
`min_exceedances`), el p-value empírico queda en el piso 1/(B+1) y BH con decenas de miles de
pruebas no puede descubrir nada. Se ajusta entonces una distribución de Pareto generalizada a
las `n_exceed` réplicas más grandes y se extrapola la cola. Como en el paper, si el ajuste no
pasa una prueba de bondad (aquí Cramér-von Mises), se reduce `n_exceed` de a 10.

Dos salvaguardas propias, verificadas en tests/test_detect_fdr.py:
- `n_exceed` se limita a un cuarto de las réplicas: con pocas réplicas el "extremo" sería casi
  toda la distribución y la extrapolación da p diminutos falsos bajo el nulo.
- Forma >= 0: si el ajuste da una cola acotada (forma < 0), el estadístico observado suele
  caer fuera del soporte y el p sale ~0. Se usa entonces la cola exponencial (forma 0), que
  para colas livianas es conservadora.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np
from scipy import stats

# p mínimo informado cuando la GPD ajustada tiene soporte acotado y el estadístico lo supera.
P_FLOOR_GPD = 1e-12


@dataclass(frozen=True)
class TailConfig:
    method: str = "gpd"  # "gpd" o "none" (solo p empírico)
    min_exceedances: int = 10
    n_exceed: int = 250
    n_exceed_min: int = 50
    gof_alpha: float = 0.05


def empirical_p(observed: float, null: np.ndarray) -> tuple[float, int]:
    """p = (1 + #{null >= obs}) / (B + 1) y el número de excedencias."""
    exceed = int(np.count_nonzero(null >= observed))
    return (1 + exceed) / (null.size + 1), exceed


def gpd_tail_p(observed: float, null: np.ndarray, cfg: TailConfig) -> float | None:
    """p extrapolado con una GPD sobre la cola de `null`; None si ningún ajuste es aceptable."""
    srt = np.sort(null)[::-1]
    b = srt.size
    n_exc = min(cfg.n_exceed, b // 4)
    while n_exc >= cfg.n_exceed_min:
        threshold = (srt[n_exc - 1] + srt[n_exc]) / 2.0
        excess = srt[:n_exc] - threshold
        if np.ptp(excess) <= 0:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            shape, _, scale = stats.genpareto.fit(excess, floc=0.0)
            if not np.isfinite(shape) or shape < 0:
                shape, scale = 0.0, float(excess.mean())
            gof = stats.cramervonmises(excess, "genpareto", args=(shape, 0.0, scale))
        if np.isfinite(shape) and scale > 0 and gof.pvalue > cfg.gof_alpha:
            tail = float(stats.genpareto.sf(observed - threshold, shape, 0.0, scale))
            return max(n_exc / b * tail, P_FLOOR_GPD)
        n_exc -= 10
    return None


def bootstrap_p(observed: float, null: np.ndarray, cfg: TailConfig) -> tuple[float, str]:
    """p-value de una cola (estadístico grande = evidencia) y el método que lo produjo."""
    null = np.asarray(null, dtype=float)
    p, exceed = empirical_p(observed, null)
    if cfg.method == "gpd" and exceed < cfg.min_exceedances:
        tail = gpd_tail_p(observed, null, cfg)
        if tail is not None:
            # La extrapolación nunca supera al empírico con su +1 (que es conservador).
            return min(tail, p), "gpd"
    return p, "empirical"


def bh_adjust(p: np.ndarray) -> np.ndarray:
    """q-values de Benjamini-Hochberg (p ajustados, monótonos, en [0, 1])."""
    p = np.asarray(p, dtype=float)
    m = p.size
    if m == 0:
        return p.copy()
    order = np.argsort(p)
    ranked = p[order] * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.clip(q, 0.0, 1.0)
    return out


def by_adjust(p: np.ndarray) -> np.ndarray:
    """q-values de Benjamini-Yekutieli: BH multiplicado por c(m) = sum 1/i."""
    p = np.asarray(p, dtype=float)
    if p.size == 0:
        return p.copy()
    c_m = float(np.sum(1.0 / np.arange(1, p.size + 1)))
    return np.clip(bh_adjust(p) * c_m, 0.0, 1.0)
