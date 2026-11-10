"""Crecimiento relativo al agregado para series jóvenes (`relative_growth`, ADR 0017).

Las series de 3 a 15 años no llegan a la ventana de `slope` (5 + 5 años con línea base) ni a la
historia de `regime`. Este detector no mira su pendiente propia: compara su crecimiento
logarítmico con el de su agregado padre (misma encuesta o tabla, mismo NAICS con menos dígitos, o
el total de la encuesta; ver `parents.py`) y pregunta si la diferencia d_t = g_serie − g_padre es
sostenida.

Estadístico, sobre las d_t del periodo común (en la grilla de la serie):
    T = |media(d)|  si la racha más larga de d por encima de |media|/2 (con el signo de la
        media) tiene ≥ k periodos
        (k = persistencia de la frecuencia, config `persistence`), y 0 si no.
La persistencia está DENTRO del estadístico, como en `slope`: un salto aislado da T = 0 y p = 1.
Hipótesis nula: la serie crece como su padre (E[d] = 0). El nulo es un stationary bootstrap sobre
d centrada (post-blackening AR(1), `src.stats.bootstrap`) con los MISMOS índices de bloques para
todas las parejas de una familia (grilla y largo); p con la cola GPD de los demás detectores. Una
familia FDR propia por corrida (BH decide, BY compara); no toca la calibración de `slope` ni de
`regime`.

`effect` = media(d) / escala robusta de d (MAD): comparable entre parejas.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from src.detect.numeric import DetectorResult, _longest_run
from src.stats.bootstrap import (
    AR_BURN_IN,
    ar_null,
    family_block_length,
    fit_ar1,
    stationary_indices,
)
from src.stats.fdr import TailConfig, bootstrap_p
from src.stats.transform import MAD_TO_SD

DETECTOR = "relative_growth"


@dataclass(frozen=True)
class RelParams:
    n_resamples: int
    block_length: int | str
    seed: int
    phi_margin: float
    tail: TailConfig


@dataclass(frozen=True)
class Pair:
    """Una serie joven emparejada con su padre, ya en su grilla."""

    d: np.ndarray  # diferencia de crecimientos (serie − padre) en el periodo común
    last_index: int  # periodo de la grilla de la última diferencia
    persistence: int  # k, en periodos de la grilla
    grid: str


def difference(
    g_child: np.ndarray, last_child: int, g_parent: np.ndarray, last_parent: int, min_periods: int
) -> tuple[np.ndarray, int] | None:
    """d = g_hijo − g_padre en el periodo común (ambos terminan en `last_*`); None si es corto."""
    last = min(last_child, last_parent)
    first = max(last_child - g_child.size + 1, last_parent - g_parent.size + 1)
    n = last - first + 1
    if n < min_periods:
        return None
    c = g_child[(first - (last_child - g_child.size + 1)) :][:n]
    p = g_parent[(first - (last_parent - g_parent.size + 1)) :][:n]
    return c - p, last


def relative_stat(d: np.ndarray, persistence: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(T, media, persistente) de diferencias `d` (..., n)."""
    mean = d.mean(axis=-1)
    side = np.sign(mean)[..., None]
    # La racha cuenta periodos con una diferencia de al menos la mitad de la media: ruido
    # simétrico alrededor de cero no la sostiene, y un salto aislado tampoco.
    persistent = _longest_run(side * d > np.abs(mean)[..., None] / 2) >= persistence
    return np.where(persistent, np.abs(mean), 0.0), mean, persistent


def scale_of(d: np.ndarray) -> float:
    """Escala robusta (MAD) de las diferencias; 0 si es degenerada."""
    med = np.median(d)
    return float(np.median(np.abs(d - med)) * MAD_TO_SD)


def relative_test(
    d: np.ndarray,
    idx: np.ndarray,
    persistence: int,
    params: RelParams,
) -> DetectorResult | None:
    """Prueba de una pareja con índices compartidos `idx` (B, n + AR_BURN_IN). None si degenera."""
    scale = scale_of(d)
    if not np.isfinite(scale) or scale <= 0:
        return None
    t_obs, mean, pers = relative_stat(d, persistence)
    centered = d - d.mean()
    null = ar_null(centered, idx, fit_ar1(centered, params.phi_margin))
    t_null, _, _ = relative_stat(null, persistence)
    if float(t_obs) == 0.0:
        p, method = 1.0, "empirical"
    else:
        p, method = bootstrap_p(float(t_obs), t_null, params.tail)
    return DetectorResult(
        statistic=float(t_obs),
        effect=float(mean) / scale,
        persistent=bool(pers),
        p_value=p,
        p_method=method,
        break_pos=0,
    )


def run_family(
    pairs: Sequence[Pair], params: RelParams, selected: Sequence[bool] | None = None
) -> tuple[list[DetectorResult | None], dict[tuple[str, int], float]]:
    """Resultado por pareja y longitudes de bloque por (grilla, largo): índices compartidos.

    `selected` calcula solo un shard; los bloques salen siempre de la familia completa."""
    groups: dict[tuple[str, int], list[int]] = defaultdict(list)
    for i, p in enumerate(pairs):
        groups[(p.grid, p.d.size)].append(i)
    out: list[DetectorResult | None] = [None] * len(pairs)
    blocks: dict[tuple[str, int], float] = {}
    per_year = {"M": 12, "Q": 4, "A": 1}
    for (grid, n), pos in sorted(groups.items()):
        block = family_block_length([pairs[i].d for i in pos], params.block_length)
        blocks[(grid, n)] = block
        idx = stationary_indices(
            n, params.n_resamples, block, (params.seed, per_year[grid], n), n_out=n + AR_BURN_IN
        )
        for i in pos:
            if selected is None or selected[i]:
                out[i] = relative_test(pairs[i].d, idx, pairs[i].persistence, params)
    return out, blocks
