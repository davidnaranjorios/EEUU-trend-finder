"""Ejecución del detector `relative_growth` sobre las series jóvenes (ADR 0017).

Carga las series jóvenes (`run.load_young_series`), busca el padre de cada una (`parents.py`),
prepara el crecimiento de ambas en la grilla de la joven y prueba la diferencia
(`relative.py`). Escribe en `detector_tests` con `detector = "relative_growth"`: `finalize` cierra
su familia FDR propia (BH decide, BY compara) sin tocar las de slope y regime.
Cada serie joven que no se prueba deja su motivo en el resumen.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import duckdb

from src.catalog import connect
from src.config import REPO_ROOT
from src.detect import run as drun
from src.detect.parents import Key, Meta, find_parent, index_by_family, own_code, units_of_catalog
from src.detect.relative import DETECTOR, Pair, RelParams, difference, run_family
from src.ingest.store import bucket_of
from src.rank import naics as naics_mod
from src.stats.fdr import TailConfig
from src.stats.transform import PERIODS_PER_YEAR, grid_date, persistence_in_grid, prepare


def rel_params(cfg: dict[str, Any]) -> RelParams:
    cal = cfg["fdr"]["calibration"]
    return RelParams(
        n_resamples=cal["n_resamples"],
        block_length=cal["block_length"],
        seed=cal["seed"],
        phi_margin=float(cfg["detect"]["relative_growth"]["phi_margin"]),
        tail=TailConfig(**cfg["detect"]["tail"]),
    )


def load_meta(con: duckdb.DuckDBPyConnection, series: dict[Key, drun.Series]) -> dict[Key, Meta]:
    """Metadatos de las series (título, ruta de categoría, unidades, ajuste y frecuencia)."""
    import pyarrow as pa

    keys = sorted(series)
    con.register(
        "_ykeys", pa.table({"source": [k[0] for k in keys], "series_id": [k[1] for k in keys]})
    )
    rows = con.execute(
        "SELECT k.source, k.series_id, any_value(m.title), any_value(m.category_path) "
        "FROM _ykeys k LEFT JOIN series_meta m USING (source, series_id) GROUP BY ALL"
    ).fetchall()
    out = {}
    for src, sid, title, path in rows:
        s = series[(src, sid)]
        out[(src, sid)] = Meta(
            src,
            sid,
            title or "",
            path or "",
            s.units or "",
            s.seasonal_adjustment or "",
            s.frequency,
        )
    return out


def lineage_path(cfg: dict[str, Any], run_id: str) -> Path:
    return drun.table_root(cfg, "reports") / "relative_growth" / f"{run_id}.json"


def save_lineage(cfg: dict[str, Any], run_id: str, lineage: dict[str, dict[str, Any]]) -> None:
    """Linaje de las series jóvenes: padre elegido (y cómo) o motivo de no probarlas.

    Idéntico en todos los shards (cada uno lo deriva de los mismos datos): se reescribe igual."""
    path = lineage_path(cfg, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(lineage.items())), indent=1), "utf-8")


def run_young(
    cfg: dict[str, Any],
    run_id: str,
    shard: tuple[int, int] | None = None,
    log: Any = None,
) -> dict[str, Any]:
    """Prueba las series jóvenes contra su padre y guarda `detector_tests` sin q (aún)."""
    d = cfg["detect"]
    con = connect(drun._resolve(cfg["paths"]["processed_dir"]))
    young = drun.load_young_series(con, cfg)
    stats: dict[str, Any] = {"young_loaded": len(young)}
    if not young:
        con.close()
        return stats
    series = {(s.source, s.series_id): s for s in [*drun.load_series(con, cfg), *young]}
    meta = load_meta(con, series)
    con.close()
    tables = naics_mod.load_tables(cfg, REPO_ROOT)
    links = naics_mod.series_links(
        {k: f"{m.title} | {m.category_path}" for k, m in meta.items()}, tables
    )
    universe = {k: (m, own_code(links.get(k))) for k, m in meta.items()}
    by_unit = index_by_family(universe)

    pairs: list[Pair] = []
    owners: list[tuple[drun.Series, Key, str]] = []
    reasons: Counter[str] = Counter()
    how: Counter[str] = Counter()
    lineage: dict[str, dict[str, Any]] = {}  # joven -> padre elegido o motivo de no probarla
    rg = d["relative_growth"]
    for s in sorted(young, key=lambda x: (x.source, x.series_id)):
        child = meta[(s.source, s.series_id)]
        cand_keys = {k for u in units_of_catalog(child) for k in by_unit.get(u, [])}
        parent, reason = find_parent(child, {k: universe[k] for k in cand_keys}, tables)
        ckey = f"{s.source}|{s.series_id}"
        if parent is None:
            reasons[reason or "sin_padre"] += 1
            lineage[ckey] = {"not_tested": reason or "sin_padre"}
            continue
        pser = series[parent.key]
        if pser.grid != s.grid:
            reasons["grilla_distinta"] += 1
            lineage[ckey] = {"not_tested": "grilla_distinta"}
            continue
        per_year = PERIODS_PER_YEAR[s.grid]
        prepped = []
        for x in (s, pser):
            n = min(x.level.size - 1, d["lookback_years"] * per_year)
            prepped.append(
                prepare(
                    x.level,
                    x.first_index,
                    n,
                    x.grid,
                    seasonal_adjust=(x.seasonal_adjustment or "").startswith(
                        drun.SEASONAL_UNADJUSTED_PREFIX
                    ),
                    winsor_mad=d["winsor_mad"],
                    max_gap_share=d["max_gap_share"],
                )
            )
        if prepped[0] is None or prepped[1] is None:
            reasons["crecimiento_no_preparable"] += 1
            lineage[ckey] = {"not_tested": "crecimiento_no_preparable"}
            continue
        diff = difference(
            prepped[0].growth,
            prepped[0].last_index,
            prepped[1].growth,
            prepped[1].last_index,
            rg["min_periods"][s.grid],
        )
        if diff is None:
            reasons["pocos_periodos_comunes"] += 1
            lineage[ckey] = {"not_tested": "pocos_periodos_comunes"}
            continue
        k = persistence_in_grid(
            s.frequency, cfg["persistence"]["min_consecutive_periods"][s.frequency]
        )
        pairs.append(Pair(diff[0], diff[1], k, s.grid))
        owners.append((s, parent.key, parent.how))
        how[parent.how] += 1
        lineage[ckey] = {
            "parent": f"{parent.key[0]}|{parent.key[1]}",
            "how": parent.how,
            "parent_naics": parent.code,
            "child_naics": universe[(s.source, s.series_id)][1],
            "periods": int(diff[0].size),
        }

    selected = None
    if shard is not None:
        selected = [
            bucket_of(f"{s.source}|{s.series_id}", shard[1]) == shard[0] for s, _, _ in owners
        ]
    results, blocks = run_family(pairs, rel_params(cfg), selected)
    rows = []
    for pair, (s, _parent, _kind), res in zip(pairs, owners, results, strict=True):
        if res is None:
            continue
        n = pair.d.size
        rows.append(
            {
                "run_id": run_id,
                "detector": DETECTOR,
                "source": s.source,
                "object_id": s.series_id,
                "frequency": s.frequency,
                "grid": s.grid,
                "n_growth": n,
                "block_length": blocks[(s.grid, n)],
                "statistic": res.statistic,
                "effect_size": res.effect,
                "persistent": res.persistent,
                "p_value": res.p_value,
                "p_method": res.p_method,
                "q_value": None,
                "q_by": None,
                "n_tests": None,
                "window_start": grid_date(pair.last_index - n + 1, s.grid),
                "window_end": grid_date(pair.last_index, s.grid),
                "copyrighted": s.copyrighted,
            }
        )
    drun.write_by_source(
        drun.DETECTOR_TESTS, rows, drun.table_root(cfg, "detector_tests"), "object_id"
    )
    save_lineage(cfg, run_id, lineage)
    stats.update(
        {
            "with_parent": len(pairs),
            "tested": len(rows),
            "parent_by_method": dict(how),
            "not_tested_by_reason": dict(sorted(reasons.items())),
            "confidence": rg["confidence"],
        }
    )
    if log is not None:
        log.info("series jóvenes probadas", extra={"fields": {"detector": DETECTOR, **stats}})
    return stats


__all__ = ["rel_params", "run_young"]
