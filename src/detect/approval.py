"""Tasa de aprobación BH por detector y alerta de desbalance (revisión 2a02c981).

Tasa = n_bh / n_tests de la última corrida de cada detector. Un detector se marca cuando su tasa
supera `factor` (3×) la mediana de las tasas de los demás: un detector que aprueba mucho más que
el resto suele estar midiendo un artefacto (deriva, boilerplate), no tendencias.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median
from typing import Any


def rate(n_bh: int, n_tests: int) -> float:
    return n_bh / n_tests if n_tests else 0.0


def failures(rows: Sequence[dict[str, Any]]) -> list[str]:
    """Detectores con pruebas y 0 aprobaciones: se reportan aparte como "posible falla"."""
    return [r["detector"] for r in rows if r["n_tests"] > 0 and r["n_bh"] == 0]


def alert_flags(
    rows: Sequence[dict[str, Any]], factor: float = 3.0, min_rate: float = 0.001
) -> dict[str, bool]:
    """detector -> alerta. `rows`: {detector, n_bh, n_tests}, una por detector.

    Con un solo detector no hay con qué comparar (sin alerta). Si la mediana de los demás es 0,
    alerta solo si la tasa supera `min_rate` (evita alertar por una señal suelta)."""
    rates = {r["detector"]: rate(r["n_bh"], r["n_tests"]) for r in rows}
    # Un detector sin aprobaciones no entra a la mediana: arrastraría la referencia a 0.
    failed = set(failures(rows))
    out: dict[str, bool] = {}
    for det, r in rates.items():
        others = [v for d, v in rates.items() if d != det and d not in failed]
        if not others:
            out[det] = False
            continue
        med = median(others)
        out[det] = r > factor * med and r > (min_rate if med == 0 else 0.0)
    return out


def annotate(
    rows: Sequence[dict[str, Any]], factor: float = 3.0, min_rate: float = 0.001
) -> list[dict[str, Any]]:
    flags = alert_flags(rows, factor, min_rate)
    return [
        {
            **r,
            "bh_rate": rate(r["n_bh"], r["n_tests"]),
            "alert": flags[r["detector"]],
            "possible_failure": r["detector"] in set(failures(rows)),
        }
        for r in rows
    ]


def latest_runs(cfg: dict[str, Any], partitions: Sequence[str]) -> list[dict[str, Any]]:
    """Última fila de `detector_runs` por (partición, detector); un detector por familia."""
    from src.detect.run import table_root
    from src.ingest.store import read_table
    from src.schemas import DETECTOR_RUNS

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for part in partitions:
        for r in read_table(
            DETECTOR_RUNS, table_root(cfg, "detector_runs"), source=part
        ).to_pylist():
            key = (part, r["detector"])
            if key not in latest or r["started_at"] > latest[key]["started_at"]:
                latest[key] = {**r, "family": part}
    return list(latest.values())
