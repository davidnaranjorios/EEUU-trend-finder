"""Presupuesto duro de las fuentes (ADR 0019): se corta DENTRO de una unidad grande, deja estado
consistente, la corrida siguiente continúa, y el `timeout` del script no frena a las demás."""

from __future__ import annotations

import copy
import json
import logging
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.config import REPO_ROOT, ConfigError, load_config, validate_config
from src.ingest.common import (
    EXIT_WATCHDOG,
    SourceIngestor,
    SourcePaths,
    pending,
    rss_mb,
    start_watchdog,
)
from src.ingest.store import read_table
from src.schemas import SERIES
from tests.fakes_synthetic import SyntheticAdapter

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
# Series objetivo: U1.NAC y U2.NAC (321 meses cada una; U1.NAC.NSA es una variante excluida).
FULL_ROWS = 2 * 321


class Clock:
    """Reloj falso: avanza solo cuando el adaptador "lee" (así una unidad lenta es determinista)."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class SlowAdapter(SyntheticAdapter):
    """Cada fila de U1 cuesta `row_cost` segundos del reloj falso (U2 es instantánea)."""

    def __init__(self, clock: Clock, row_cost: float = 1.0) -> None:
        super().__init__()
        self.clock, self.row_cost = clock, row_cost

    def fetch_values(self, unit, series_ids):
        for row in super().fetch_values(unit, series_ids):
            if unit.unit_id == "U1":
                self.clock.t += self.row_cost
            yield row


def _setup(tmp_path, **scfg):
    cfg = copy.deepcopy(load_config())
    cfg["download"]["groups"] = ["nacional"]
    cfg["sources"]["synthetic"] = {
        **copy.deepcopy(cfg["sources"]["bls"]),
        "lineage_aggregate_min_series": 3,
        "deadline_check_rows": 50,
        "write_batch_rows": 200,
        **scfg,
    }
    clock = Clock()
    adapter = SlowAdapter(clock)
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", "synthetic")
    ing = SourceIngestor(adapter, cfg, paths, now=lambda: NOW, clock=clock)
    ing.crawl(full=True, run_id="r1")
    return ing, adapter, paths, clock


def _rows(paths):
    t = read_table(SERIES, paths.series, source="synthetic")
    return sorted((r["series_id"], r["date"]) for r in t.to_pylist())


def test_a_slow_unit_is_cut_inside_the_unit_when_the_budget_runs_out(tmp_path):
    ing, adapter, paths, clock = _setup(tmp_path)
    stats = ing.download(budget_s=100)  # U1 necesita ~321 s de lectura: sin corte dentro, sería eso
    assert stats["units_interrupted"] == 1
    assert (
        stats.get("units_fetched", 0) == 0 and stats["units_pending"] == 2
    )  # U1 cortada, U2 pendiente
    assert pending(stats)  # código de salida 10: queda trabajo para la próxima corrida
    assert clock.t < 200  # se detuvo cerca del presupuesto, no al terminar la unidad
    # Estado consistente: la unidad cortada NO quedó como hecha.
    assert ing._unit_state("values") == {}
    assert set(ing._unit_state("values_interrupted")) == {"U1"}
    # Lo ya escrito son upserts completos (sin filas duplicadas ni a medias).
    rows = _rows(paths)
    assert len(rows) == len(set(rows))


def test_next_run_resumes_serves_the_other_units_first_and_ends_complete(tmp_path):
    ing, adapter, paths, clock = _setup(tmp_path)
    ing.download(budget_s=100)
    first = list(adapter.fetches)
    stats = ing.download()  # sin presupuesto: continúa
    assert adapter.fetches[len(first) :] == ["U2", "U1"]  # la interrumpida pasa al final
    assert stats["units_fetched"] == 2 and stats["units_pending"] == 0 and not pending(stats)
    rows = _rows(paths)
    assert len(rows) == FULL_ROWS and len(rows) == len(set(rows))  # sin duplicar nada
    assert set(ing._unit_state("values")) == {"U1", "U2"}
    # Una tercera corrida no vuelve a pedir nada.
    before = list(adapter.fetches)
    again = ing.download()
    assert adapter.fetches == before and again["units_unchanged"] == 2


def test_the_unit_cap_stops_a_huge_unit_without_starving_the_rest(tmp_path):
    ing, adapter, paths, clock = _setup(tmp_path, unit_max_min=1)  # tope de 60 s por unidad
    stats = ing.download()  # sin presupuesto de corrida: solo manda el tope de la unidad
    assert stats["units_interrupted"] == 1 and stats["units_fetched"] == 1  # U2 sí corrió
    assert set(ing._unit_state("values")) == {"U2"}
    assert {r[0] for r in _rows(paths)} >= {"U2.NAC"}


def test_the_budget_also_applies_while_writing(tmp_path):
    ing, adapter, paths, clock = _setup(tmp_path, deadline_check_rows=10_000_000)
    # Sin comprobación por filas, el corte llega al primer lote escrito (200 filas).
    stats = ing.download(budget_s=100)
    assert stats["units_interrupted"] == 1 and clock.t <= 250


def test_crawl_budget_is_hard_inside_a_unit(tmp_path):
    ing, adapter, paths, clock = _setup(tmp_path)
    original = adapter.catalog_rows

    def slow_catalog(unit):
        for row in original(unit):
            clock.t += 100.0
            yield row

    adapter.catalog_rows = slow_catalog
    stats = ing.crawl(full=True, run_id="r2", budget_s=150)
    assert stats["units_interrupted"] >= 1 and stats["units_pending"] >= 1
    assert pending({"crawl": stats})
    # Un crawl completo posterior deja todo hecho.
    adapter.catalog_rows = original
    done = ing.crawl(full=True, run_id="r3")
    assert done.get("units_pending", 0) == 0 and done.get("units_interrupted", 0) == 0


def test_per_unit_and_per_phase_logs(tmp_path, caplog):
    ing, adapter, paths, clock = _setup(tmp_path, progress_rows=100)
    with caplog.at_level(logging.INFO):
        ing.download()
    msgs = [r.getMessage() for r in caplog.records]
    for m in ("unidad iniciada", "unidad en progreso", "unidad terminada"):
        assert m in msgs, m
    end = next(r for r in caplog.records if r.getMessage() == "unidad terminada")
    fields = end.fields
    assert {"unit", "filas_escritas", "lectura_s", "escritura_s", "seconds", "rss_mb"} <= set(
        fields
    )
    ing.crawl(full=True, run_id="r4")
    with caplog.at_level(logging.INFO):
        ing.crawl(full=True, run_id="r5")
    assert any(r.getMessage() == "crawl: unidad terminada" for r in caplog.records)


def test_duplicates_are_deduplicated_per_batch_and_across_batches_idempotently(tmp_path):
    """La memoria ya no crece con la unidad (el conjunto de claves se vacía en cada lote): una
    observación repetida en otro lote es un upsert idempotente, no un duplicado."""
    ing, adapter, paths, clock = _setup(tmp_path, write_batch_rows=50)
    base = adapter.fetch_values

    def twice(unit, series_ids):
        rows = list(base(unit, series_ids))
        yield from rows
        yield from rows  # todo repetido, mucho después (otro lote)

    adapter.fetch_values = twice
    ing.download()
    rows = _rows(paths)
    assert len(rows) == FULL_ROWS and len(rows) == len(set(rows))


# --- watchdog y utilidades ----------------------------------------------------------------


def test_watchdog_fires_after_budget_plus_grace_and_can_be_cancelled():
    log = logging.getLogger("test-watchdog")
    fired: list[int] = []
    t = start_watchdog(0.05, 0.05, log, exit_fn=fired.append)
    time.sleep(0.5)
    assert fired == [EXIT_WATCHDOG]
    ok: list[int] = []
    t2 = start_watchdog(0.2, 0.2, log, exit_fn=ok.append)
    t2.cancel()
    time.sleep(0.6)
    assert ok == [] and t is not None
    assert start_watchdog(None, 10, log) is None  # sin presupuesto no hay watchdog


def test_rss_is_reported_on_linux():
    assert rss_mb() is None or rss_mb() > 0


def test_config_validates_the_budget_options():
    cfg = json.loads(json.dumps(load_config()))
    validate_config(cfg)
    for key, bad in (("unit_max_min", 0), ("hard_grace_min", -1), ("progress_rows", 0)):
        broken = json.loads(json.dumps(cfg))
        broken["sources"]["bls"][key] = bad
        with pytest.raises(ConfigError):
            validate_config(broken)


# --- script: timeout por fuente, la sync y las demás fuentes siguen --------------------------


def _script_env(tmp_path, behavior: dict[str, str], sync_fails: set[str] | None = None):
    """Copia de infra/ con `uv` y `sync_r2.sh` falsos. `behavior`: fuente -> hang | pending | ok |
    fail. `sync_fails`: fuentes cuya sync devuelve error."""
    shutil.copytree(REPO_ROOT / "infra", tmp_path / "infra")
    calls = tmp_path / "calls.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cases = "\n".join(f'  *"src.ingest.{src}"*) {code};;' for src, code in _codes(behavior).items())
    (bin_dir / "uv").write_text(
        f"""#!/usr/bin/env bash
echo "uv $*" >> "{calls}"
case "$*" in
{cases}
esac
"""
    )
    (bin_dir / "uv").chmod(0o755)
    fail_pat = "|".join(f"*{s}*" for s in (sync_fails or {"__ninguna__"}))
    (tmp_path / "infra" / "sync_r2.sh").write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "sync $*" >> "{calls}"
case "$*" in {fail_pat}) exit 3;; esac
"""
    )
    (tmp_path / "infra" / "sync_r2.sh").chmod(0o755)
    return bin_dir, calls


def _codes(behavior):
    out = {}
    for src, what in behavior.items():
        out[src] = {"hang": "sleep 30", "pending": "exit 10", "ok": "exit 0", "fail": "exit 2"}[
            what
        ]
    return out


def _run_script(tmp_path, bin_dir, env_extra, *sources):
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SOURCES_BUDGET_S": "1",
        "SOURCES_GRACE_S": "1",
        "SOURCES_SYNC_TIMEOUT_S": "10",
        "SOURCES_MIN_USEFUL_S": "0",
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(tmp_path / "infra" / "sources_update.sh"), *sources],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
    )


def test_script_timeout_kills_a_hung_source_syncs_what_was_done_and_runs_the_rest(tmp_path):
    bin_dir, calls = _script_env(tmp_path, {"bls": "hang", "bea": "pending", "census": "ok"})
    t0 = time.monotonic()
    res = _run_script(tmp_path, bin_dir, {}, "bls", "bea", "census")
    assert time.monotonic() - t0 < 30  # el timeout cortó la fuente colgada (sleep 30)
    lines = calls.read_text().splitlines()
    updates = [ln for ln in lines if ln.startswith("uv ")]
    syncs = [ln for ln in lines if ln.startswith("sync ")]
    # Las tres fuentes corrieron y las tres se sincronizaron (también la cortada), en orden.
    assert [u.split("src.ingest.")[1].split()[0] for u in updates] == ["bls", "bea", "census"]
    assert [s.split("processed/series/")[1].split()[0] for s in syncs] == ["bls", "bea", "census"]
    assert res.returncode == 1  # visible: una fuente fue cortada
    assert "bls cortada por timeout" in res.stderr and "fallaron bls(timeout)" in res.stderr
    assert "bea" not in res.stderr.split("fallaron")[-1]  # pendiente (10) no es fallo


def test_script_a_sync_failure_does_not_stop_the_other_sources(tmp_path):
    bin_dir, calls = _script_env(
        tmp_path, {"bls": "ok", "bea": "ok", "census": "ok"}, sync_fails={"series/bls"}
    )
    res = _run_script(tmp_path, bin_dir, {}, "bls", "bea", "census")
    lines = calls.read_text().splitlines()
    assert (
        sum(ln.startswith("uv ") for ln in lines) == 3
        and sum(ln.startswith("sync ") for ln in lines) == 3
    )
    assert res.returncode == 1 and "fallaron bls(sync)" in res.stderr


def test_script_pending_and_ok_exit_zero_and_a_failing_source_does_not_stop_others(tmp_path):
    bin_dir, calls = _script_env(tmp_path, {"bls": "pending", "bea": "fail", "census": "ok"})
    res = _run_script(tmp_path, bin_dir, {}, "bls", "bea", "census")
    assert res.returncode == 1 and "fallaron bea" in res.stderr
    assert sum(ln.startswith("sync ") for ln in calls.read_text().splitlines()) == 3
    bin_dir2, calls2 = _script_env(Path(tmp_path / "b"), {"bls": "pending", "census": "ok"})
    ok = _run_script(Path(tmp_path / "b"), bin_dir2, {}, "bls", "census")
    assert ok.returncode == 0, ok.stderr


def test_script_skips_a_source_without_useful_time_and_reports_it_pending(tmp_path):
    bin_dir, calls = _script_env(tmp_path, {"bls": "ok", "bea": "ok"})
    res = _run_script(
        tmp_path,
        bin_dir,
        {"SOURCES_TOTAL_S": "5", "SOURCES_MIN_USEFUL_S": "3"},  # 5 − grace(1) − sync(10) < 0
        "bls",
        "bea",
    )
    assert res.returncode == 0  # saltar por tiempo no es un fallo: queda pendiente
    assert "sin tiempo útil" in res.stderr and "pendientes para la próxima corrida" in res.stderr
    assert not calls.exists() or "uv " not in calls.read_text()


def test_script_passes_the_budget_to_the_ingestor_in_minutes():
    text = (REPO_ROOT / "infra" / "sources_update.sh").read_text()
    assert "timeout -k 60" in text and "--budget-min" in text
    assert "failed+=(" in text and "sync_rc" in text
