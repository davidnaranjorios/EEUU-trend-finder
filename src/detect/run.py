"""Corrida de los detectores numéricos sobre las series vigentes y cierre de la familia FDR.

Uso:
  uv run python -m src.detect run [--shard I/N --run-id ID] [--max-series N]
  uv run python -m src.detect fdr --run-id ID     # cierra una corrida hecha por shards
  uv run python -m src.detect report [--run-id ID]

Flujo (ADR 0007):
1. Carga los valores vigentes (`vintage_date IS NULL`) de las series de la última corrida del
   embudo de cada fuente de `detect.sources` en los grupos `download.groups`, con al menos
   `eligibility.min_obs` observaciones.
2. Prepara el crecimiento (src.stats.transform) y prueba cada serie con `slope` y `regime`
   (src.detect.family), con índices bootstrap compartidos por subfamilia.
3. Escribe una fila por prueba en `detector_tests` (linaje, con p calibrado).
4. Cierra cada familia (detector × corrida): BH decide, BY se reporta como comparación;
   fecha con PELT el último régimen de los sobrevivientes de `regime`; escribe `signals`,
   `detector_runs` y el reporte de la corrida.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa

from src.catalog import connect
from src.config import REPO_ROOT, load_config
from src.detect.approval import annotate
from src.detect.dedup import dedup_series
from src.detect.family import FamilyParams, Item, run_family
from src.detect.numeric import locate_break
from src.ingest.store import bucket_of, read_table, upsert
from src.log import get_logger, run
from src.schemas import DETECTOR_RUNS, DETECTOR_TESTS, SIGNALS
from src.stats.fdr import TailConfig, bh_adjust, by_adjust
from src.stats.transform import (
    GRID_OF,
    PERIODS_PER_YEAR,
    Prepared,
    grid_date,
    grid_index,
    persistence_in_grid,
    prepare,
    to_grid,
)

N_BUCKETS = 16
FULL_RUN_SOURCES = ("fred",)
SEASONAL_UNADJUSTED_PREFIX = "NSA"


@dataclass
class Series:
    source: str
    series_id: str
    frequency: str
    grid: str
    seasonal_adjustment: str | None
    copyrighted: bool
    level: np.ndarray
    first_index: int


@dataclass
class Prepped:
    series: Series
    prep: Prepared
    item: Item


# --- rutas ---------------------------------------------------------------------------------------


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


def table_root(cfg: dict[str, Any], name: str) -> Path:
    """Directorio de cada tabla de salida (linaje bajo processed, señales en data/signals)."""
    processed = _resolve(cfg["paths"]["processed_dir"])
    return {
        "detector_tests": processed / "lineage" / "detector_tests",
        "detector_runs": processed / "lineage" / "detector_runs",
        "signals": _resolve(cfg["paths"]["signals_dir"]),
        "reports": _resolve(cfg["paths"]["signals_dir"]) / "reports",
    }[name]


# --- carga ---------------------------------------------------------------------------------------


# Fuente de las series de referencia metodológicas (context.reference_series, ADR 0009).
REFERENCE_SOURCE = "fred"


def reference_keys(cfg: dict[str, Any]) -> set[tuple[str, str]]:
    """Series de referencia (deflactor, población, recesiones): nunca entran a detectores."""
    refs = (cfg.get("context") or {}).get("reference_series") or {}
    return {(REFERENCE_SOURCE, sid) for sid in refs.values()}


def detect_sources(cfg: dict[str, Any]) -> list[str]:
    """Fuentes numéricas que entran a la familia (una familia por detector, todas juntas)."""
    return list(cfg["detect"]["sources"])


def load_series(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    ids: list[tuple[str, str]] | None = None,
) -> list[Series]:
    """Series vigentes de las fuentes de `detect.sources`, en su grilla (D y W a mensual).

    `ids`, si se da, son pares (fuente, serie).
    """
    out: list[Series] = []
    for source in detect_sources(cfg):
        wanted = None if ids is None else [sid for src, sid in ids if src == source]
        if wanted is not None and not wanted:
            continue
        out += _load_source(con, cfg, source, wanted)
    return out


def _load_source(
    con: duckdb.DuckDBPyConnection,
    cfg: dict[str, Any],
    source: str,
    ids: list[str] | None,
) -> list[Series]:
    """Series de una fuente: última decisión del embudo por serie, grupos `download.groups`."""
    min_obs = cfg["eligibility"]["min_obs"]
    # FRED reclasifica todo el catálogo en cada corrida del embudo: vale su última corrida. Las
    # fuentes de la Fase 4 reprocesan solo las unidades que cambiaron: última decisión por serie.
    latest = (
        "run_id = max_by(run_id, decided_at) OVER ()"
        if source in FULL_RUN_SOURCES
        else "row_number() OVER (PARTITION BY series_id ORDER BY decided_at DESC, run_id DESC) = 1"
    )
    params: list[Any] = [source, cfg["download"]["groups"], source]
    id_filter = ""
    if ids is not None:
        id_filter = "AND list_contains(?, s.series_id)"
        params.append(sorted(ids))
    rel = con.execute(
        f"""
        WITH last AS (
          SELECT series_id, group_name, copyrighted FROM eligibility WHERE source = ?
          QUALIFY {latest}
        ), ok AS (
          SELECT series_id, copyrighted FROM last WHERE list_contains(?, group_name)
        )
        SELECT s.series_id,
               any_value(s.frequency) AS frequency,
               any_value(s.seasonal_adjustment) AS seasonal_adjustment,
               any_value(ok.copyrighted) AS copyrighted,
               count(*) AS n_obs,
               list(CASE WHEN s.frequency = 'Q' THEN year(s.date) * 4 + (month(s.date) - 1) // 3
                         WHEN s.frequency = 'A' THEN year(s.date)
                         ELSE year(s.date) * 12 + month(s.date) - 1 END ORDER BY s.date) AS gi,
               list(s.value ORDER BY s.date) AS vals
        FROM series s JOIN ok USING (series_id)
        WHERE s.source = ? AND s.vintage_date IS NULL AND s.value IS NOT NULL
          AND isfinite(s.value) {id_filter}
        GROUP BY s.series_id
        ORDER BY s.series_id
        """,
        params,
    ).to_arrow_table()
    out: list[Series] = []
    refs = reference_keys(cfg)
    gi_col, val_col = rel.column("gi").combine_chunks(), rel.column("vals").combine_chunks()
    gi_flat = gi_col.flatten().to_numpy(zero_copy_only=False).astype(np.int64)
    val_flat = val_col.flatten().to_numpy(zero_copy_only=False).astype(float)
    offsets = gi_col.offsets.to_numpy()
    for i, (sid, freq, sa, cr, n_obs) in enumerate(
        zip(
            rel.column("series_id").to_pylist(),
            rel.column("frequency").to_pylist(),
            rel.column("seasonal_adjustment").to_pylist(),
            rel.column("copyrighted").to_pylist(),
            rel.column("n_obs").to_pylist(),
            strict=True,
        )
    ):
        if n_obs < min_obs[freq] or (source, sid) in refs:
            continue
        lo, hi = offsets[i], offsets[i + 1]
        level, first = to_grid(gi_flat[lo:hi], val_flat[lo:hi])
        out.append(Series(source, sid, freq, GRID_OF[freq], sa, bool(cr), level, first))
    return out


def prep_for(detector: str, series: list[Series], cfg: dict[str, Any]) -> list[Prepped]:
    """Crecimiento preparado por detector: los últimos `lookback_years` (o lo que haya).

    En `slope` la línea base del nulo (historia previa a la ventana reciente) debe tener al
    menos `max(W, slope_min_baseline)` periodos; si los 15 años no alcanzan (anuales), se toma
    más historia. Con menos, el nulo no está calibrado (ADR 0007, tamaño medido en anuales).
    """
    d = cfg["detect"]
    out = []
    for s in series:
        per_year = PERIODS_PER_YEAR[s.grid]
        k = persistence_in_grid(
            s.frequency, cfg["persistence"]["min_consecutive_periods"][s.frequency]
        )
        window = d["slope_window"][s.grid]
        lookback = d["lookback_years"] * per_year
        if detector == "slope":
            minimum = window + max(window, d["slope_min_baseline"])
            lookback = max(lookback, minimum)
        else:
            minimum = 2 * k + 1
        n = min(s.level.size - 1, lookback)
        if n < minimum:
            continue
        prep = prepare(
            s.level,
            s.first_index,
            n,
            s.grid,
            seasonal_adjust=(s.seasonal_adjustment or "").startswith(SEASONAL_UNADJUSTED_PREFIX),
            winsor_mad=d["winsor_mad"],
            max_gap_share=d["max_gap_share"],
        )
        if prep is None:
            continue
        out.append(Prepped(s, prep, Item(prep.growth, prep.scale, k, s.grid)))
    return out


def family_params(cfg: dict[str, Any], grid: str) -> FamilyParams:
    d, cal = cfg["detect"], cfg["fdr"]["calibration"]
    return FamilyParams(
        n_resamples=cal["n_resamples"],
        block_length=cal["block_length"],
        seed=cal["seed"],
        window=d["slope_window"][grid],
        tail=TailConfig(**d["tail"]),
        workers=d["workers"],
        phi_margin=d["phi_margin"],
        regime_escalate=d["regime_escalate"],
    )


# --- run -----------------------------------------------------------------------------------------


def build_test_rows(
    detector: str,
    prepped: list[Prepped],
    cfg: dict[str, Any],
    run_id: str,
    shard: tuple[int, int] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filas de `detector_tests` del shard y resumen de longitudes de bloque por grilla."""
    rows: list[dict[str, Any]] = []
    blocks_summary: dict[str, Any] = {}
    for grid in sorted({p.series.grid for p in prepped}):
        group = [p for p in prepped if p.series.grid == grid]
        selected = None
        if shard is not None:
            selected = [
                bucket_of(f"{p.series.source}|{p.series.series_id}", shard[1]) == shard[0]
                for p in group
            ]
        params = family_params(cfg, grid)
        results, blocks = run_family(detector, [p.item for p in group], params, selected)
        blocks_summary[grid] = {
            "subfamilies": len(blocks),
            "block_length_median": float(np.median(list(blocks.values()))),
        }
        for p, res in zip(group, results, strict=True):
            if res is None:
                continue
            last = p.prep.last_index
            n = p.item.growth.size
            start = last - n + 1 + res.break_pos
            rows.append(
                {
                    "run_id": run_id,
                    "detector": detector,
                    "source": p.series.source,
                    "object_id": p.series.series_id,
                    "frequency": p.series.frequency,
                    "grid": grid,
                    "n_growth": n,
                    "block_length": blocks[(grid, n)],
                    "statistic": res.statistic,
                    "effect_size": res.effect,
                    "persistent": res.persistent,
                    "p_value": res.p_value,
                    "p_method": res.p_method,
                    "q_value": None,
                    "q_by": None,
                    "n_tests": None,
                    "window_start": grid_date(start, grid),
                    "window_end": grid_date(last, grid),
                    "copyrighted": p.series.copyrighted,
                }
            )
    return rows, blocks_summary


def write(spec: Any, rows: list[dict[str, Any]], root: Path, source: str, bucket_col: str) -> Any:
    table = pa.Table.from_pylist(rows, schema=spec.schema)
    return upsert(table, spec, root, source=source, n_buckets=N_BUCKETS, bucket_col=bucket_col)


def write_by_source(spec: Any, rows: list[dict[str, Any]], root: Path, bucket_col: str) -> None:
    """Escribe filas con columna `source` en la partición de cada fuente."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r["source"]].append(r)
    for source, part in sorted(groups.items()):
        write(spec, part, root, source, bucket_col)


# Partición de `detector_runs` de la familia numérica: reúne todas las fuentes de detect.sources.
RUNS_PARTITION = "numeric"


def save_dedup(
    cfg: dict[str, Any],
    run_id: str,
    n_before: int,
    n_after: int,
    groups: dict[tuple[str, str], list[tuple[str, str]]],
) -> None:
    """Resumen de la deduplicación (idéntico en todos los shards; finalize lo suma al reporte)."""
    root = table_root(cfg, "reports") / "dedup"
    root.mkdir(parents=True, exist_ok=True)
    doc = {
        "series_before": n_before,
        "series_after": n_after,
        "groups": {
            f"{k[0]}|{k[1]}": [f"{m[0]}|{m[1]}" for m in v] for k, v in sorted(groups.items())
        },
    }
    (root / f"{run_id}.json").write_text(json.dumps(doc, indent=1), "utf-8")


def load_dedup(cfg: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    path = table_root(cfg, "reports") / "dedup" / f"{run_id}.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def run_detectors(
    cfg: dict[str, Any],
    run_id: str,
    shard: tuple[int, int] | None = None,
    max_series: int | None = None,
    log: Any = None,
) -> dict[str, Any]:
    """Prueba todas las series (o un shard) y guarda `detector_tests` sin q (aún)."""
    con = connect(_resolve(cfg["paths"]["processed_dir"]))
    series = load_series(con, cfg)
    con.close()
    n_before = len(series)
    series, dup_groups = dedup_series(series)
    save_dedup(cfg, run_id, n_before, len(series), dup_groups)
    if max_series is not None:
        series = series[:max_series]
    stats: dict[str, Any] = {
        "series_loaded": len(series),
        "series_before_dedup": n_before,
        "by_source": dict(Counter(s.source for s in series)),
        "by_frequency": dict(Counter(s.frequency for s in series)),
    }
    for detector in cfg["detect"]["detectors"]:
        prepped = prep_for(detector, series, cfg)
        rows, blocks = build_test_rows(detector, prepped, cfg, run_id, shard)
        write_by_source(DETECTOR_TESTS, rows, table_root(cfg, "detector_tests"), "object_id")
        stats[detector] = {"prepared": len(prepped), "tested": len(rows), "blocks": blocks}
        if log is not None:
            log.info(
                "detector probado", extra={"fields": {"detector": detector, **stats[detector]}}
            )
    return stats


# --- fdr -----------------------------------------------------------------------------------------


def signal_id(run_id: str, detector: str, source: str, object_id: str) -> str:
    return hashlib.sha1(f"{run_id}|{detector}|{source}|{object_id}".encode()).hexdigest()[:20]


def finalize(
    cfg: dict[str, Any], run_id: str, started_at: datetime, log: Any = None
) -> dict[str, Any]:
    """Cierra las familias de `run_id`: BH/BY, PELT para fechar, signals, runs y reporte."""
    q = float(cfg["fdr"]["q"])
    tests = [
        r
        for source in detect_sources(cfg)
        for r in read_table(
            DETECTOR_TESTS,
            table_root(cfg, "detector_tests"),
            source=source,
            where="run_id = ?",
            params=[run_id],
        ).to_pylist()
    ]
    if not tests:
        raise RuntimeError(f"no hay pruebas para run_id={run_id}")
    by_det: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in tests:
        by_det[r["detector"]].append(r)

    run_date = datetime.now(UTC).date()
    signals: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "run_id": run_id,
        "run_date": run_date.isoformat(),
        "q": q,
        "sources": detect_sources(cfg),
        "detectors": {},
        "dedup": load_dedup(cfg, run_id),
    }
    for detector, rows in sorted(by_det.items()):
        p = np.array([r["p_value"] for r in rows])
        q_bh, q_by = bh_adjust(p), by_adjust(p)
        for r, a, b in zip(rows, q_bh, q_by, strict=True):
            r["q_value"], r["q_by"], r["n_tests"] = float(a), float(b), len(rows)
        survivors = [r for r in rows if r["q_value"] <= q]
        if detector == "regime" and survivors:
            _date_regimes(cfg, survivors)
        for r in survivors:
            signals.append(
                {
                    "signal_id": signal_id(run_id, detector, r["source"], r["object_id"]),
                    "source": r["source"],
                    "object_id": r["object_id"],
                    "detector": detector,
                    "window_start": r["window_start"],
                    "window_end": r["window_end"],
                    "effect_size": r["effect_size"],
                    "p_value": r["p_value"],
                    "q_value": r["q_value"],
                    "run_date": run_date,
                    "run_id": run_id,
                    "n_tests": len(rows),
                }
            )
        n_bh = len(survivors)
        n_by = sum(r["q_by"] <= q for r in rows)
        runs.append(
            {
                "run_id": run_id,
                "detector": detector,
                "started_at": started_at.replace(microsecond=0),
                "finished_at": datetime.now(UTC).replace(microsecond=0),
                "n_tests": len(rows),
                "n_bh": n_bh,
                "n_by": n_by,
                "q": q,
                "params_json": json.dumps(
                    {
                        "detect": cfg["detect"],
                        "calibration": cfg["fdr"]["calibration"],
                        "persistence": cfg["persistence"],
                    },
                    sort_keys=True,
                ),
            }
        )
        report["detectors"][detector] = summarize(rows, q)
    ac = cfg["fdr"]["approval_alert"]
    for r, a in zip(runs, annotate(runs, ac["factor"], ac["min_rate"]), strict=True):
        extra = {"bh_rate": a["bh_rate"], "approval_alert": a["alert"]}
        r["params_json"] = json.dumps({**json.loads(r["params_json"]), **extra}, sort_keys=True)
    write_by_source(DETECTOR_TESTS, tests, table_root(cfg, "detector_tests"), "object_id")
    write_by_source(SIGNALS, signals, table_root(cfg, "signals"), "object_id")
    write(DETECTOR_RUNS, runs, table_root(cfg, "detector_runs"), RUNS_PARTITION, "run_id")
    for det, a in zip(
        report["detectors"], annotate(runs, ac["factor"], ac["min_rate"]), strict=False
    ):
        report["detectors"][det]["approval_alert"] = a["alert"]
        report["detectors"][det]["possible_failure"] = a["possible_failure"]
    save_report(cfg, report)
    if log is not None:
        log.info("familias cerradas", extra={"fields": {"run_id": run_id, "signals": len(signals)}})
    return report


def _date_regimes(cfg: dict[str, Any], survivors: list[dict[str, Any]]) -> None:
    """PELT sobre el crecimiento de cada sobreviviente: inicio del último régimen y su efecto."""
    con = connect(_resolve(cfg["paths"]["processed_dir"]))
    series = load_series(con, cfg, ids=[(r["source"], r["object_id"]) for r in survivors])
    con.close()
    prepped = {(p.series.source, p.series.series_id): p for p in prep_for("regime", series, cfg)}
    pelt = cfg["detect"]["pelt"]
    for r in survivors:
        p = prepped.get((r["source"], r["object_id"]))
        if p is None:
            continue
        g, last, n = p.item.growth, p.prep.last_index, p.item.growth.size
        fallback = grid_index(r["window_start"], p.series.grid) - (last - n + 1)
        b, effect = locate_break(
            g, p.item.scale, p.item.persistence, pelt["jump"], pelt["penalty"], fallback
        )
        r["window_start"] = grid_date(last - n + 1 + b, p.series.grid)
        r["effect_size"] = effect


# --- reporte -------------------------------------------------------------------------------------


def summarize(rows: list[dict[str, Any]], q: float) -> dict[str, Any]:
    """Conteos de la familia: pruebas, BH, BY, por frecuencia nativa y método de p."""
    by_freq: dict[str, dict[str, int]] = defaultdict(lambda: {"tests": 0, "bh": 0, "by": 0})
    by_src: dict[str, dict[str, int]] = defaultdict(lambda: {"tests": 0, "bh": 0, "by": 0})
    for r in rows:
        for f in (by_freq[r["frequency"]], by_src[r["source"]]):
            f["tests"] += 1
            f["bh"] += int(r["q_value"] <= q)
            f["by"] += int(r["q_by"] <= q)
    bh = [r for r in rows if r["q_value"] <= q]
    # Resolución de p: el p mínimo posible por método (piso empírico 1/(B+1)) frente al umbral
    # mínimo de BH, q/m. Si el piso supera q/m, BH no puede aprobar nada aunque la señal sea fuerte.
    p_min = min((r["p_value"] for r in rows), default=1.0)
    return {
        "p_min": p_min,
        "bh_min_p_needed": q / len(rows) if rows else None,
        "p_floor_blocks_bh": bool(rows) and p_min > q / len(rows),
        "n_tests": len(rows),
        "n_bh": len(bh),
        "n_by": sum(r["q_by"] <= q for r in rows),
        "bh_rate": len(bh) / len(rows) if rows else 0.0,
        "by_frequency": dict(sorted(by_freq.items())),
        "by_source": dict(sorted(by_src.items())),
        "p_method": dict(Counter(r["p_method"] for r in rows)),
        "bh_copyrighted": sum(r["copyrighted"] for r in bh),
        "bh_increase": sum(r["effect_size"] > 0 for r in bh),
        "bh_decrease": sum(r["effect_size"] < 0 for r in bh),
    }


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Detectores numéricos — corrida {report['run_id']}",
        "",
        f"Fecha: {report['run_date']} · q = {report['q']} · BH decide; BY es solo comparación.",
        "",
        "| Detector | Pruebas | BH | % BH | BY | Suben | Bajan | Con copyright | Método del p |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    dd = report.get("dedup")
    if dd:
        lines.insert(
            4,
            f"Deduplicación previa a las pruebas: {dd['series_before']} series antes, "
            f"{dd['series_after']} después ({len(dd['groups'])} grupos de variables repetidas).\n",
        )
    for det, s in report["detectors"].items():
        flags = (" ⚠" if s.get("approval_alert") else "") + (
            " ❓ posible falla" if s.get("possible_failure") else ""
        )
        lines.append(
            f"| {det} | {s['n_tests']} | {s['n_bh']} | {s['bh_rate']:.2%}"
            f"{flags} | {s['n_by']} | {s['bh_increase']} | "
            f"{s['bh_decrease']} | {s['bh_copyrighted']} | "
            + ", ".join(f"{k} {v}" for k, v in sorted(s["p_method"].items()))
            + " |"
        )
    lines += [
        "",
        "## Por frecuencia nativa",
        "",
        "| Detector | Frec. | Pruebas | BH | BY |",
        "|---|---|---|---|---|",
    ]
    for det, s in report["detectors"].items():
        for f, c in s["by_frequency"].items():
            lines.append(f"| {det} | {f} | {c['tests']} | {c['bh']} | {c['by']} |")
    lines += [
        "",
        "## Por fuente",
        "",
        "| Detector | Fuente | Pruebas | BH | BY |",
        "|---|---|---|---|---|",
    ]
    for det, s in report["detectors"].items():
        for f, c in s.get("by_source", {}).items():
            lines.append(f"| {det} | {f} | {c['tests']} | {c['bh']} | {c['by']} |")
    return "\n".join(lines) + "\n"


def save_report(cfg: dict[str, Any], report: dict[str, Any]) -> Path:
    root = table_root(cfg, "reports")
    root.mkdir(parents=True, exist_ok=True)
    stem = f"{report['run_date']}_{report['run_id']}"
    (root / f"{stem}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), "utf-8")
    path = root / f"{stem}.md"
    path.write_text(to_markdown(report), "utf-8")
    return path


def latest_report(cfg: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    root = table_root(cfg, "reports")
    files = sorted(root.glob(f"*_{run_id or ''}*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise RuntimeError("no hay reportes de detectores")
    return json.loads(files[-1].read_text("utf-8"))


# --- CLI -----------------------------------------------------------------------------------------


def parse_shard(value: str) -> tuple[int, int]:
    i, n = (int(x) for x in value.split("/"))
    if not 0 <= i < n:
        raise argparse.ArgumentTypeError("--shard I/N con 0 <= I < N")
    return i, n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.detect")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="prueba las series; sin --shard también cierra la familia")
    r.add_argument("--shard", type=parse_shard, default=None)
    r.add_argument("--run-id", default=None, help="obligatorio con --shard")
    r.add_argument("--max-series", type=int, default=None, help="solo para pruebas acotadas")
    f = sub.add_parser("fdr", help="cierra una corrida hecha por shards")
    f.add_argument("--run-id", required=True)
    rep = sub.add_parser("report", help="imprime el reporte (markdown) de la última corrida")
    rep.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)
    cfg = load_config()

    if args.cmd == "report":
        print(to_markdown(latest_report(cfg, args.run_id)))
        return 0
    if args.cmd == "run" and args.shard is not None and not args.run_id:
        parser.error("--shard requiere --run-id (el mismo en todos los shards)")

    log = get_logger("detect", cfg["logging"]["level"])
    with run(detect_sources(cfg), _resolve(cfg["logging"]["runs_file"])) as record:
        if getattr(args, "run_id", None):
            record.run_id = args.run_id
        details: dict[str, Any] = {"command": f"detect {args.cmd}"}
        if args.cmd == "run":
            with record.stage("detect"):
                details["stats"] = run_detectors(
                    cfg, record.run_id, args.shard, args.max_series, log
                )
            record.n_series.update(details["stats"]["by_source"])
        if args.cmd == "fdr" or (args.cmd == "run" and args.shard is None):
            with record.stage("fdr"):
                details["report"] = finalize(cfg, record.run_id, record.started_at, log)
            print(to_markdown(details["report"]))
        record.details = details
    print(json.dumps({"run_id": record.run_id}), file=sys.stderr)
    return 0
