"""Corrida completa sobre Parquet sintético: tablas, idempotencia, shards y reporte BH/BY."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime

import numpy as np
import pyarrow as pa
import pytest

from src.catalog import connect
from src.config import load_config
from src.detect.run import finalize, run_detectors, table_root, to_markdown
from src.ingest.store import upsert
from src.schemas import ELIGIBILITY, SERIES

N_NULL = 24
N_TREND = 3
N_MONTHS = 240


def _cfg(tmp_path):
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["processed_dir"] = str(tmp_path / "data" / "processed")
    cfg["paths"]["signals_dir"] = str(tmp_path / "data" / "signals")
    cfg["logging"]["runs_file"] = str(tmp_path / "data" / "logs" / "runs.jsonl")
    cfg["detect"]["workers"] = 1
    return cfg


def _month(i: int) -> date:
    return date(2006 + i // 12, i % 12 + 1, 1)


def _write_fixture(cfg) -> list[str]:
    rng = np.random.default_rng(42)
    rows, elig, trend_ids = [], [], []
    for k in range(N_NULL + N_TREND):
        sid = f"syn{k:03d}"
        g = 0.002 + 0.01 * rng.standard_normal(N_MONTHS)
        if k < N_TREND:
            g[-60:] += 0.03  # aceleración sostenida de 5 años
            trend_ids.append(sid)
        level = 100 * np.exp(np.cumsum(g))
        rows += [
            {
                "source": "fred",
                "series_id": sid,
                "date": _month(i),
                "value": float(v),
                "vintage_date": None,
                "frequency": "M",
                "units": "u",
                "seasonal_adjustment": "SA",
            }
            for i, v in enumerate(level)
        ]
        elig.append(
            {
                "run_id": "elig1",
                "source": "fred",
                "series_id": sid,
                "group_name": "nacional",
                "rule": "clasificada",
                "detail": None,
                "geo_level": "nacional",
                "frequency": "M",
                "copyrighted": k == 1,
                "decided_at": datetime(2026, 11, 1, tzinfo=UTC),
            }
        )
    processed = table_root(cfg, "detector_tests").parent.parent
    upsert(
        pa.Table.from_pylist(rows, schema=SERIES.schema),
        SERIES,
        processed / "series",
        source="fred",
        n_buckets=4,
    )
    upsert(
        pa.Table.from_pylist(elig, schema=ELIGIBILITY.schema),
        ELIGIBILITY,
        processed / "lineage" / "eligibility",
        source="fred",
        n_buckets=4,
    )
    return trend_ids


@pytest.fixture
def setup(tmp_path):
    cfg = _cfg(tmp_path)
    return cfg, _write_fixture(cfg)


def _counts(cfg):
    con = connect(cfg["paths"]["processed_dir"])
    out = {
        t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("detector_tests", "detector_runs", "signals")
    }
    return con, out


def test_run_reports_bh_and_by_counts(setup):
    cfg, trend_ids = setup
    started = datetime.now(UTC)
    stats = run_detectors(cfg, "runA")
    assert stats["series_loaded"] == N_NULL + N_TREND
    report = finalize(cfg, "runA", started)

    con, counts = _counts(cfg)
    assert counts["detector_tests"] == 2 * (N_NULL + N_TREND)
    runs = con.execute(
        "SELECT detector, n_tests, n_bh, n_by, q FROM detector_runs ORDER BY detector"
    ).fetchall()
    assert [r[0] for r in runs] == ["regime", "slope"]
    for det, n_tests, n_bh, n_by, q in runs:
        assert n_tests == N_NULL + N_TREND and q == 0.05
        assert 0 <= n_by <= n_bh  # BY es más estricto que BH
        assert report["detectors"][det]["n_bh"] == n_bh
        assert report["detectors"][det]["n_by"] == n_by
    md = to_markdown(report)
    assert "| BH | BY |" in md and "Por frecuencia" in md

    # Las tendencias inyectadas salen como señal y cada señal registra n_tests.
    sig = con.execute("SELECT detector, object_id, n_tests, q_value FROM signals").fetchall()
    found = {(d, o) for d, o, _, _ in sig}
    for det in ("slope", "regime"):
        assert {(det, t) for t in trend_ids} <= found
    assert all(n == N_NULL + N_TREND and qv <= 0.05 for _, _, n, qv in sig)


def test_rerun_is_idempotent_and_shards_match(setup):
    cfg, _ = setup
    started = datetime.now(UTC)
    run_detectors(cfg, "runB")
    finalize(cfg, "runB", started)
    _, first = _counts(cfg)
    run_detectors(cfg, "runB")
    finalize(cfg, "runB", started)
    _, second = _counts(cfg)
    assert first == second

    # Dos shards con el mismo run_id dan los mismos p que la corrida completa.
    for i in range(2):
        run_detectors(cfg, "runC", shard=(i, 2))
    finalize(cfg, "runC", started)
    con, _ = _counts(cfg)
    diff = con.execute(
        "SELECT count(*) FROM detector_tests b JOIN detector_tests c USING (detector, object_id) "
        "WHERE b.run_id = 'runB' AND c.run_id = 'runC' AND b.p_value <> c.p_value"
    ).fetchone()[0]
    n_c = con.execute("SELECT count(*) FROM detector_tests WHERE run_id = 'runC'").fetchone()[0]
    assert diff == 0 and n_c == 2 * (N_NULL + N_TREND)
