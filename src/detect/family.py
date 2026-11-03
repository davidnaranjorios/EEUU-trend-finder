"""Prueba de una familia de series con índices bootstrap compartidos y cómputo en paralelo.

La familia FDR es el detector en la corrida. Dentro de ella, las series se agrupan por grilla y
largo (subfamilias): todas las de una subfamilia usan **la misma** matriz de índices, generada
desde `seed` y la forma del problema, así que cualquier proceso o shard la reproduce.
"""

from __future__ import annotations

import atexit
import multiprocessing
import os
import zlib
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from src.detect.numeric import (
    DetectorResult,
    Escalation,
    null_sample,
    regime_test,
    slope_test,
)
from src.stats.bootstrap import AR_BURN_IN, family_block_length, stationary_indices
from src.stats.fdr import TailConfig


@dataclass(frozen=True)
class FamilyParams:
    n_resamples: int = 999
    block_length: int | str = "auto"
    seed: int = 0
    window: int = 60  # solo slope
    phi_margin: float = 1.0  # margen conservador de φ en el post-blackening
    regime_escalate: int = 0  # réplicas para las pruebas de regime en el piso (0 = no)
    tail: TailConfig = field(default_factory=TailConfig)
    workers: int = 1


@dataclass(frozen=True)
class Item:
    """Una serie preparada: crecimiento, escala robusta, persistencia exigida y grilla."""

    growth: np.ndarray
    scale: float
    persistence: int
    grid: str


_POOLS: dict[int, ProcessPoolExecutor] = {}


def _pool(workers: int) -> ProcessPoolExecutor:
    """Un pool por número de procesos, reutilizado entre familias.

    spawn y no fork: el proceso padre tiene hilos (DuckDB, BLAS) y fork puede bloquearse.
    """
    if workers not in _POOLS:
        ctx = multiprocessing.get_context("spawn")
        _POOLS[workers] = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
    return _POOLS[workers]


@atexit.register
def _shutdown_pools() -> None:
    for pool in _POOLS.values():
        pool.shutdown(cancel_futures=True)
    _POOLS.clear()


def seed_key(seed: int, detector: str, grid: str, n: int) -> list[int]:
    """Semilla de una subfamilia: estable entre procesos (no usa hash() de Python)."""
    return [seed, zlib.crc32(detector.encode()), zlib.crc32(grid.encode()), n]


def subfamilies(items: Sequence[Item]) -> dict[tuple[str, int], list[int]]:
    """Posiciones de `items` por (grilla, largo)."""
    groups: dict[tuple[str, int], list[int]] = defaultdict(list)
    for pos, it in enumerate(items):
        groups[(it.grid, it.growth.size)].append(pos)
    return dict(groups)


def block_lengths(
    detector: str, items: Sequence[Item], params: FamilyParams
) -> dict[tuple[str, int], float]:
    """Longitud de bloque por subfamilia: mediana de las óptimas de lo que se remuestrea."""
    return {
        key: family_block_length(
            [null_sample(detector, items[p].growth, params.window) for p in pos],
            params.block_length,
        )
        for key, pos in subfamilies(items).items()
    }


def _run_chunk(
    detector: str,
    chunk: list[Item],
    idx: np.ndarray,
    params: FamilyParams,
    escalation: Escalation | None = None,
) -> list[DetectorResult]:
    if detector == "slope":
        return [
            slope_test(
                it.growth,
                it.scale,
                idx,
                params.window,
                it.persistence,
                params.tail,
                params.phi_margin,
            )
            for it in chunk
        ]
    return [
        regime_test(
            it.growth, it.scale, idx, it.persistence, params.tail, params.phi_margin, escalation
        )
        for it in chunk
    ]


def run_family(
    detector: str,
    items: Sequence[Item],
    params: FamilyParams,
    selected: Sequence[bool] | None = None,
    blocks: dict[tuple[str, int], float] | None = None,
) -> tuple[list[DetectorResult | None], dict[tuple[str, int], float]]:
    """Resultado por serie (None si no está en `selected`) y longitudes de bloque usadas.

    `selected` permite calcular solo un shard; las longitudes de bloque se calculan siempre
    sobre la familia completa para que todos los shards coincidan.
    """
    if blocks is None:
        blocks = block_lengths(detector, items, params)
    out: list[DetectorResult | None] = [None] * len(items)
    tasks = []
    for key, positions in subfamilies(items).items():
        pos = [p for p in positions if selected is None or selected[p]]
        if not pos:
            continue
        grid, n = key
        if detector == "slope":  # réplicas de 2·W armadas sobre la línea base de n - W
            n_src, n_out = n - params.window, 2 * params.window
        else:
            n_src, n_out = n, n
        idx = stationary_indices(
            n_src,
            params.n_resamples,
            blocks[key],
            seed_key(params.seed, detector, grid, n),
            n_out=n_out + AR_BURN_IN,
        )
        escalation = None
        if detector == "regime" and params.regime_escalate > 0:
            escalation = Escalation(
                params.regime_escalate,
                blocks[key],
                tuple(seed_key(params.seed, detector, grid, n)),
            )
        # Trozos chicos para repartir carga entre procesos; acotados para limitar memoria.
        step = min(64, max(1, len(pos) // (4 * max(params.workers, 1))))
        for i in range(0, len(pos), step):
            part = pos[i : i + step]
            tasks.append((part, [items[p] for p in part], idx, escalation))

    workers = min(params.workers, os.cpu_count() or 1)
    if workers > 1 and len(tasks) > 1:
        ex = _pool(workers)
        futures = [
            (part, ex.submit(_run_chunk, detector, chunk, idx, params, esc))
            for part, chunk, idx, esc in tasks
        ]
        for part, fut in futures:
            for p, res in zip(part, fut.result(), strict=True):
                out[p] = res
    else:
        for part, chunk, idx, esc in tasks:
            for p, res in zip(part, _run_chunk(detector, chunk, idx, params, esc), strict=True):
                out[p] = res
    return out, blocks
