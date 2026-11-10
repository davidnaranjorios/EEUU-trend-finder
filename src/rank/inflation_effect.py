"""Etiqueta "efecto inflación" por cluster (ADR 0015; aprobada por David el 2026-11-10).

Análoga al efecto de tasas (`rate_effect.py`): un cluster de series nominales puede "acelerar"
solo porque subió la inflación. La inflación sale del deflactor ya aprobado
(`context.reference_series.deflator`, IPC-U): no hay claves nuevas, no entra a detectores ni al
ranking, y la etiqueta nunca filtra ni cambia el score: solo avisa.

Es el mismo motor que el efecto de tasas, con otro regresor:

    g_t = a + Σ_{k=0..L} b_k·π_{t−k} + e_t,   π_t = inflación del periodo (suma de las
    variaciones logarítmicas mensuales del deflactor dentro del periodo de la grilla).

- (a) relación: R² y p por stationary bootstrap con índices compartidos por familia, cola GPD y
  BH sobre los clusters (q = fdr.q). Como la inflación es persistente (un ajuste en niveles puede
  ser espurio), también se exige el MISMO modelo en primeras diferencias (Δg sobre Δπ) con su
  propio bootstrap y familia BH: la condición de diferencias del efecto de tasas.
- (b) cambio de pendiente: la prueba `slope` de los detectores sobre el residuo (g sin la parte
  lineal explicada por π). Se marca si el cambio original es significativo y el efecto cae
  ≥ `min_drop` o deja de ser significativo.

`inflation_share` = fracción del cambio de pendiente que explica la inflación. Sin el deflactor, con
historia corta o en grilla anual, el cluster no se evalúa (`inflation_effect` = NULL) y `reason`
dice por qué. Se reutilizan el resultado (`RateResult`) y las razones de `rate_effect`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import numpy as np

from src.rank import rate_effect as rx

PER_YEAR = rx.PER_YEAR


def inflation_by_grid(levels: Mapping[int, float], grid: str) -> dict[int, float]:
    """Inflación por periodo completo de la grilla: suma de las variaciones log mensuales.

    `levels`: índice de precios por mes (año·12 + mes − 1). Solo periodos con todos sus meses."""
    step = 12 // PER_YEAR[grid]
    acc: dict[int, list[float]] = defaultdict(list)
    for m, v in levels.items():
        prev = levels.get(m - 1)
        if prev is not None and prev > 0 and v > 0:
            acc[m // step].append(float(np.log(v / prev)))
    return {i: float(sum(x)) for i, x in acc.items() if len(x) == step}


def params_from(cfg: dict[str, Any]) -> rx.RateParams:
    """Parámetros del efecto inflación: los de `rate_effect` con `rank.inflation_effect` encima."""
    base = rx.params_from(cfg)
    ic = cfg["rank"]["inflation_effect"]
    return replace(
        base,
        max_lag=ic["max_lag"],
        min_drop=ic["min_drop"],
        seed=ic["seed"],
        include_level=False,  # la inflación ya es una tasa de cambio: no hay nivel que añadir
        min_rows=dict(ic.get("min_rows_by_grid") or base.min_rows),
    )


def evaluate(
    clusters: Mapping[str, tuple[np.ndarray, int, str, float, int]],
    levels: Mapping[int, float],
    p: rx.RateParams,
) -> dict[str, rx.RateResult]:
    """Evalúa todos los clusters contra la inflación del deflactor (`levels`: índice mensual)."""
    infl = {grid: inflation_by_grid(levels, grid) for grid in ("M", "Q", "A")}
    out = rx.evaluate(clusters, infl, p)
    for r in out.values():
        if r.reason == "sin_tasa":
            r.reason = "sin_deflactor"
    return out
