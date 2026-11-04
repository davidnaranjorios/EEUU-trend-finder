"""Capa común de fuentes: linaje agregado, derrame a disco, reanudación y embudo."""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from src.config import load_config
from src.eligibility import classify_rows
from src.ingest.common import (
    SourceIngestor,
    SourcePaths,
    Unit,
    expand_aggregated,
    latest_decisions,
    lineage_series_count,
    pending,
)
from tests.fakes_synthetic import SyntheticAdapter, _row

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _ing(tmp_path, adapter=None, **scfg):
    cfg = copy.deepcopy(load_config())
    cfg["download"]["groups"] = ["nacional"]
    cfg["sources"]["synthetic"] = {**copy.deepcopy(cfg["sources"]["bls"]), **scfg}
    adapter = adapter or SyntheticAdapter()
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", "synthetic")
    return SourceIngestor(adapter, cfg, paths, now=lambda: NOW), paths


def test_dead_unit_gets_one_aggregated_lineage_row(tmp_path):
    ing, paths = _ing(tmp_path, lineage_aggregate_min_series=3)
    stats = ing.crawl(full=True, run_id="r1")
    assert stats["units_aggregated"] == 1
    dead = [d for d in latest_decisions(paths, "synthetic") if d["series_id"] == "DEAD/*"]
    assert len(dead) == 1 and dead[0]["rule"] == "inactiva"
    assert dead[0]["detail"] == "n_series=5;max=ultimo_dato=2010-12-01;version=v1"
    assert lineage_series_count(dead[0]) == 5


def test_aggregated_row_expands_to_per_series_decisions(tmp_path):
    """La fila agregada es reproducible: desde la unidad versionada se obtienen exactamente
    las mismas decisiones que con una fila por serie."""
    adapter = SyntheticAdapter()
    agg, p_agg = _ing(tmp_path / "agg", adapter, lineage_aggregate_min_series=3)
    per, p_per = _ing(tmp_path / "per", adapter, lineage_aggregate_min_series=None)
    agg.crawl(full=True, run_id="r")
    per.crawl(full=True, run_id="r")
    row = next(d for d in latest_decisions(p_agg, "synthetic") if d["series_id"] == "DEAD/*")
    unit = next(u for u in adapter.list_units() if u.unit_id == "DEAD")
    expanded = expand_aggregated(adapter, unit, row, agg.cfg, NOW.date())
    fields = ("series_id", "group_name", "rule", "detail", "frequency", "copyrighted")
    per_series = [
        {k: d[k] for k in fields}
        for d in latest_decisions(p_per, "synthetic")
        if d["series_id"].startswith("DEAD.")
    ]
    assert [{k: d[k] for k in fields} for d in expanded] == per_series
    # Si el archivo cambió de versión, no se expande con datos de otro archivo.
    with pytest.raises(ValueError, match="versión"):
        expand_aggregated(adapter, Unit("DEAD", "v2"), row, agg.cfg, NOW.date())


def test_aggregation_off_writes_one_row_per_series(tmp_path):
    ing, paths = _ing(tmp_path, lineage_aggregate_min_series=None)
    ing.crawl(full=True, run_id="r1")
    ids = {d["series_id"] for d in latest_decisions(paths, "synthetic")}
    assert {f"DEAD.{i}" for i in range(5)} <= ids and "DEAD/*" not in ids


def test_spill_gives_same_decisions_as_memory(tmp_path):
    big = SyntheticAdapter(n_dead=0)
    # 300 series activas con títulos repetidos (variantes) en una unidad.
    rows = [
        _row(
            f"B.{i}",
            f"T{i % 40}",
            date(2000, 1, 1),
            date(2026, 9, 1),
            "nacional",
            sa=("SA", "NSA")[i % 2],
        )
        for i in range(300)
    ]
    big.catalog_rows = lambda unit: iter(rows) if unit.unit_id == "U2" else iter(())
    ing_mem, p_mem = _ing(tmp_path / "mem", big, spill_rows=10_000)
    ing_disk, p_disk = _ing(tmp_path / "disk", big, spill_rows=16)
    ing_mem.crawl(full=True, run_id="r")
    ing_disk.crawl(full=True, run_id="r")

    def key(p):
        return sorted(
            (d["series_id"], d["rule"], d["detail"]) for d in latest_decisions(p, "synthetic")
        )

    assert key(p_mem) == key(p_disk)
    expected = classify_rows([{**r, "source": "synthetic"} for r in rows], ing_mem.cfg, NOW.date())
    assert sorted((d["series_id"], d["rule"], d["detail"]) for d in expected) == key(p_mem)


def test_download_resumes_by_unit(tmp_path):
    adapter = SyntheticAdapter()
    ing, paths = _ing(tmp_path, adapter)
    ing.crawl(full=True, run_id="r1")
    first = ing.download(max_units=1)
    assert first["units_fetched"] == 1 and first["units_pending"] == 1
    assert not paths.initial_done.exists()
    second = ing.download()
    assert second["units_fetched"] == 1 and second["units_pending"] == 0
    assert paths.initial_done.exists()
    assert adapter.fetches == ["U1", "U2"]  # nada se pidió dos veces


def test_incremental_crawl_skips_unchanged_units(tmp_path):
    adapter = SyntheticAdapter()
    ing, _ = _ing(tmp_path, adapter)
    ing.crawl(full=True, run_id="r1")
    again = ing.crawl(full=False, run_id="r2")
    assert again["units_unchanged"] == 3 and again.get("units_processed", 0) == 0
    adapter.version += 1
    third = ing.crawl(full=False, run_id="r3")
    assert third["units_processed"] == 1


def test_estimate_counts_by_group_and_rule(tmp_path):
    ing, _ = _ing(tmp_path, lineage_aggregate_min_series=3)
    ing.crawl(full=True, run_id="r1")
    est = ing.estimate()
    # U1.NAC y U1.NAC.NSA comparten título: una es variante. U1.REG es regional.
    assert est["grupos"] == {"excluida": 6, "nacional": 2, "regional": 1}
    assert est["excluidas_por_regla"] == {"inactiva": 5, "variante": 1}
    nac = est["tamano_por_grupo"]["nacional"]
    assert nac["series"] == 2 and nac["unidades"] == 2 and nac["obs_estimadas"] > 600


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 10.0  # cada consulta del reloj avanza 10 s
        return self.t


def test_crawl_budget_leaves_units_pending_and_resumes(tmp_path):
    adapter = SyntheticAdapter()
    ing, _ = _ing(tmp_path, adapter)
    ing._clock = _Clock()
    first = ing.crawl(run_id="r1", budget_s=15)
    assert first["units_processed"] >= 1 and first["units_pending"] >= 1
    assert pending({"crawl": first})
    ing._clock = _Clock()
    second = ing.crawl(run_id="r2")
    assert second["units_processed"] == first["units_pending"]
    assert second["units_unchanged"] == first["units_processed"]


class _CountingVersions(SyntheticAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.version_calls = 0

    def values_version(self, unit: Unit) -> str | None:
        self.version_calls += 1
        return super().values_version(unit)


def test_download_without_budget_does_not_query_versions(tmp_path):
    """En la corrida real, BEA gastó 12 min pidiendo versiones de 436 unidades que igual
    quedaron pendientes: sin presupuesto no se consulta nada."""
    adapter = _CountingVersions()
    ing, _ = _ing(tmp_path, adapter)
    ing.crawl(run_id="r1")
    out = ing.download(budget_s=0.0)
    assert adapter.version_calls == 0
    assert out["units_pending"] == out["target_units"] > 0


def test_update_reserves_budget_for_download_once_targets_exist(tmp_path):
    """El crawl no se come todo el presupuesto cuando ya hay series que descargar."""
    adapter = SyntheticAdapter()
    ing, _ = _ing(tmp_path, adapter, crawl_budget_share=0.5)
    first = ing.update(budget_s=10_000)  # sin objetivos aún: el crawl usa todo
    assert first["crawl"]["units_processed"] == 3

    seen: list[float | None] = []
    crawl = ing.crawl

    def spy(**kw):
        seen.append(kw.get("budget_s"))
        return crawl(**kw)

    ing.crawl = spy
    second = ing.update(budget_s=100.0, run_id="r2")
    assert seen == [50.0]
    assert "units_pending" in second["download"]


def test_old_classification_is_refreshed(tmp_path):
    adapter = SyntheticAdapter()
    ing, _ = _ing(tmp_path, adapter, catalog_refresh_days=30)
    ing.crawl(run_id="r1")
    later = NOW + timedelta(days=31)
    ing._now = lambda: later
    assert ing.crawl(run_id="r2")["units_processed"] == 3


class _Flaky(SyntheticAdapter):
    """Corta la conexión a mitad del catálogo y de los valores de U1 `cuts` veces."""

    def __init__(self, cuts: int) -> None:
        super().__init__()
        self.cuts = {"catalog": cuts, "values": cuts}

    def _cut(self, kind: str, unit: Unit) -> bool:
        if unit.unit_id == "U1" and self.cuts[kind] > 0:
            self.cuts[kind] -= 1
            return True
        return False

    def catalog_rows(self, unit: Unit):
        cut = self._cut("catalog", unit)
        for i, r in enumerate(super().catalog_rows(unit)):
            if cut and i == 1:
                raise httpx.ReadError("Connection reset by peer")
            yield r

    def fetch_values(self, unit: Unit, series_ids: set[str]):
        cut = self._cut("values", unit)
        for i, r in enumerate(super().fetch_values(unit, series_ids)):
            if cut and i == 10:
                raise httpx.ReadError("Connection reset by peer")
            yield r


def _flaky_ing(tmp_path, adapter):
    ing, paths = _ing(tmp_path, adapter, max_retries=2, lineage_aggregate_min_series=3)
    ing._sleep = lambda s: None
    return ing, paths


def test_connection_cut_mid_file_repeats_the_unit(tmp_path):
    """Un corte a mitad de archivo (pasó en vivo con BLS) repite la unidad: mismo resultado."""
    ing, paths = _flaky_ing(tmp_path / "flaky", _Flaky(cuts=2))
    ref, p_ref = _ing(tmp_path / "ref", lineage_aggregate_min_series=3)
    a = ing.crawl(full=True, run_id="r")
    b = ref.crawl(full=True, run_id="r")
    assert a == b and "units_failed" not in a
    key = lambda d: (d["series_id"], d["group_name"], d["rule"])  # noqa: E731
    assert sorted(map(key, latest_decisions(paths, "synthetic"))) == sorted(
        map(key, latest_decisions(p_ref, "synthetic"))
    )
    da, db = ing.download(), ref.download()
    assert da["rows_inserted"] == db["rows_inserted"] and da["units_pending"] == 0


def test_persistent_cut_leaves_unit_pending_and_source_continues(tmp_path):
    ing, paths = _flaky_ing(tmp_path, _Flaky(cuts=99))
    stats = ing.crawl(full=True, run_id="r")
    assert stats["units_failed"] == 1 and stats["units_processed"] == 2
    assert pending(stats)
    # La unidad fallida no tiene estado: la próxima corrida la retoma.
    ing.adapter.cuts = {"catalog": 0, "values": 0}
    again = ing.crawl(run_id="r2")
    assert again["units_processed"] == 1 and again["units_unchanged"] == 2
