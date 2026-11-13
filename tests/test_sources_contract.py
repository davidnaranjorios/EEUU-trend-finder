"""Criterio de la Fase 4: cada fuente pasa el mismo test de esquema e idempotencia.

Cada fuente aporta `tests/fakes_<fuente>.py` con `make_adapter(tmp_path, cfg)` (respuestas
grabadas, sin red) y `bump(adapter)` (una publicación nueva). El mismo test corre sobre todas.
"""

from __future__ import annotations

import copy
import importlib
from datetime import UTC, datetime

import pytest

from src.config import load_config
from src.ingest.common import (
    SOURCE_CATALOG,
    SourceIngestor,
    SourcePaths,
    latest_decisions,
    lineage_series_count,
)
from src.ingest.store import read_table
from src.schemas import ELIGIBILITY, ELIGIBILITY_GROUPS, SERIES, SERIES_META, validate

SOURCES = ["synthetic", "bls", "bea", "census", "cbp", "bds", "econcensus"]
NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _cfg(source: str) -> dict:
    cfg = copy.deepcopy(load_config())
    cfg["download"]["groups"] = ["nacional"]
    if source not in cfg["sources"]:
        cfg["sources"][source] = copy.deepcopy(cfg["sources"]["bls"])
    cfg["sources"][source]["lineage_aggregate_min_series"] = 3
    return cfg


def _setup(source, tmp_path):
    fakes = importlib.import_module(f"tests.fakes_{source}")
    cfg = _cfg(source)
    adapter = fakes.make_adapter(tmp_path, cfg)
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", adapter.source)
    ing = SourceIngestor(adapter, cfg, paths, now=lambda: NOW)
    return fakes, adapter, ing, paths


def _tables(paths, source):
    return {
        spec.name: read_table(spec, root, source=source)
        for spec, root in (
            (SERIES, paths.series),
            (SERIES_META, paths.series_meta),
            (ELIGIBILITY, paths.eligibility),
            (SOURCE_CATALOG, paths.catalog),
        )
    }


@pytest.mark.parametrize("source", SOURCES)
def test_schema_lineage_and_idempotence(source, tmp_path):
    fakes, adapter, ing, paths = _setup(source, tmp_path)
    assert adapter.source == source

    n_catalog = sum(sum(1 for _ in adapter.catalog_rows(u)) for u in adapter.list_units())
    crawl = ing.crawl(full=True, run_id="r1")
    assert crawl["series"] == n_catalog > 0
    first_dl = ing.download()
    assert first_dl["units_pending"] == 0 and first_dl["rows_inserted"] > 0

    t = _tables(paths, adapter.source)
    for spec in (SERIES, SERIES_META, ELIGIBILITY, SOURCE_CATALOG):
        validate(t[spec.name], spec)  # esquema común (y catálogo neutral)
    series = t["series"].to_pylist()
    assert {r["source"] for r in series} == {adapter.source}
    assert all(r["vintage_date"] is None for r in series)  # sin vintages (decisión 5)
    assert not any(m["copyrighted"] for m in t["series_meta"].to_pylist())

    # Linaje: cada serie del catálogo tiene exactamente una decisión (o una fila agregada).
    decisions = latest_decisions(paths, adapter.source)
    assert sum(lineage_series_count(d) for d in decisions) == n_catalog
    assert {d["group_name"] for d in decisions} <= set(ELIGIBILITY_GROUPS)
    # Objetivo = lo que descarga el ingestor: los grupos de `download.groups` y las series nuevas
    # (las fuentes de mercado con pocos años, como el Censo Económico, caen en «nuevas»).
    targets = set(ing.targets())
    assert targets and {r["series_id"] for r in series} <= targets
    assert {r["series_id"] for r in series} == targets  # todas las objetivo tienen valores

    # Idempotencia: repetir crawl y download no agrega ni cambia filas de datos.
    ing.crawl(full=False, run_id="r2")
    again = ing.download()
    assert again["rows_inserted"] == 0 and again["rows_replaced"] == 0
    # Aunque se pierda el estado por unidad, volver a bajar todo tampoco duplica.
    for f in paths.unit_state.rglob("*.parquet"):
        f.unlink()
    forced = ing.download()
    assert forced["units_fetched"] > 0
    assert forced["rows_inserted"] == 0 and forced["rows_replaced"] == 0
    t2 = _tables(paths, adapter.source)
    for name in ("series", "series_meta", "source_catalog"):
        assert t2[name].num_rows == t[name].num_rows, name
        assert (
            t2[name]
            .sort_by([(c, "ascending") for c in t2[name].column_names if c != "last_updated"][:3])
            .equals(
                t[name].sort_by(
                    [(c, "ascending") for c in t[name].column_names if c != "last_updated"][:3]
                )
            )
        ), name


@pytest.mark.parametrize("source", SOURCES)
def test_new_release_is_incremental(source, tmp_path):
    fakes, adapter, ing, paths = _setup(source, tmp_path)
    ing.crawl(full=True, run_id="r1")
    ing.download()
    before = read_table(SERIES, paths.series, source=adapter.source).num_rows
    fakes.bump(adapter)
    ing.crawl(full=False, run_id="r2")
    res = ing.download()
    assert res["units_fetched"] >= 1
    assert res["rows_inserted"] == 1 and res["rows_replaced"] == 1
    after = read_table(SERIES, paths.series, source=adapter.source)
    validate(after, SERIES)
    assert after.num_rows == before + 1
