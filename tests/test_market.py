"""Bloque 2 (ADR 0024): fuentes de mercado por NAICS y tabla derivada `naics_metrics`."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import pyarrow.parquet as pq
import pytest

from src.config import REPO_ROOT, ConfigError, load_config, validate_config
from src.ingest.common import SourceIngestor, SourcePaths
from src.market.build import build, size_upper
from src.rank import naics
from tests import fakes_market as fm

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
MAKERS = {"cbp": fm.make_cbp, "bds": fm.make_bds, "econcensus": fm.make_econcensus}


def _cfg():
    cfg = copy.deepcopy(load_config())
    cfg["download"]["groups"] = ["nacional"]
    return cfg


def _ingest_all(tmp_path, cfg):
    adapters = {}
    for src, make in MAKERS.items():
        adapter = make(tmp_path, cfg)
        paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", src)
        ing = SourceIngestor(adapter, cfg, paths, now=lambda: NOW)
        ing.crawl(full=True, run_id="r1")
        ing.download()
        adapters[src] = adapter
    return adapters


def test_market_sources_never_enter_the_detectors():
    cfg = load_config()
    assert set(cfg["detect"]["sources"]).isdisjoint(MAKERS)
    assert set(MAKERS) <= set(cfg["sources"])  # sí están en la configuración de ingesta
    bad = copy.deepcopy(cfg)
    bad["detect"]["sources"] = [*bad["detect"]["sources"], "cbp"]
    with pytest.raises(ConfigError, match="mercado"):
        validate_config(bad)


def test_discovery_uses_variables_json_and_skips_sic_and_old_vintages(tmp_path):
    cfg = _cfg()
    cbp = fm.make_cbp(tmp_path, cfg)
    ids = set(cbp._series())
    # Todo el árbol que devolvió la API, ningún código escrito a mano; relleno de espacios limpio.
    assert {"cbp:ESTAB:00", "cbp:ESTAB:62", "cbp:EMP:621", "cbp:PAYANN:6231"} <= ids
    assert not any("99X" in i for i in ids)  # código que no es NAICS
    calls = [r.url.path for r in cbp.fake.requests]  # type: ignore[attr-defined]
    assert "/data/1990/cbp" not in calls  # vintage con SIC: se salta sin pedir datos
    assert cbp._series()["cbp:ESTAB:6231"].obs.keys() == {
        __import__("datetime").date(y, 1, 1) for y in range(2008, 2024)
    }
    ec = fm.make_econcensus(tmp_path, cfg)
    ec._series()
    assert not any("2012" in r.url.path and "variables" not in r.url.path for r in ec.fake.requests)  # type: ignore[attr-defined]


def test_econcensus_concentration_and_size_series(tmp_path):
    ec = fm.make_econcensus(tmp_path, _cfg())
    s = ec._series()
    assert s["econcensus:CR4:6231"].units == "Percent"
    assert "4 largest firms" in s["econcensus:CR4:6231"].title
    assert "econcensus:CR8:6231" in s
    assert "econcensus:CR1:6231" not in s and not any(":CR0" in i for i in s)  # «All firms»
    assert "econcensus:FIRM_SIZE:6231:510" in s
    assert "econcensus:FIRM_SIZE:6231:001" in s  # clase total, se filtra al derivar
    assert s["econcensus:RCPTOT:6231"].units == "Thousands of dollars"
    assert len({d.year for d in s["econcensus:RCPTOT:6231"].obs}) == 2  # 2017 y 2022


def test_ignore_staleness_keeps_annual_reference_data(tmp_path):
    cfg = _cfg()
    cbp = fm.make_cbp(tmp_path, cfg)
    ing = SourceIngestor(
        cbp, cfg, SourcePaths(tmp_path / "processed", tmp_path / "raw", "cbp"), now=lambda: NOW
    )
    stats = ing.crawl(full=True, run_id="r1")
    assert stats.get("regla_inactiva", 0) == 0 and stats["grupo_nacional"] > 0
    cfg2 = copy.deepcopy(cfg)
    cfg2["sources"]["cbp"]["ignore_staleness"] = False
    cbp2 = fm.make_cbp(tmp_path / "x", cfg2)
    ing2 = SourceIngestor(
        cbp2,
        cfg2,
        SourcePaths(tmp_path / "x" / "processed", tmp_path / "x" / "raw", "cbp"),
        now=lambda: NOW,
    )
    assert ing2.crawl(full=True, run_id="r1").get("regla_inactiva", 0) > 0


def test_size_class_upper_bound_from_official_label():
    assert size_upper("x, Firms operated entire year with less than 5 employees, NAICS 6231") == 4
    assert size_upper("x, Firms ... with 10 to 19 employees, NAICS 6231") == 19
    assert size_upper("x, Firms ... with 500 employees or more, NAICS 6231") == float("inf")
    assert size_upper("x, All firms, NAICS 6231") is None


def test_build_naics_metrics_on_fixtures(tmp_path):
    cfg = _cfg()
    _ingest_all(tmp_path, cfg)
    stats = build(cfg, tmp_path / "processed")
    assert {"firms", "establishments", "employment", "revenue", "cr4", "cr8", "cr20"} - set(
        stats["metrics"]
    ) == {"cr20"}  # el fake no trae CR20
    rows = pq.read_table(tmp_path / "processed/market/naics_metrics/part-0.parquet").to_pylist()
    by = {(r["naics"], r["metric"], r["year"]): r["value"] for r in rows}
    assert len(by) == len(rows)  # clave (naics, metric, year) única
    assert by[("6231", "establishments", 2023)] == 400 + 23
    assert by[("6231", "firms", 2022)] == 120 + 22
    assert by[("6231", "cr4", 2022)] == 8.5 and by[("6231", "cr8", 2022)] == 11.5
    assert by[("6231", "entry_rate", 2023)] == pytest.approx(12.23)
    assert by[("6231", "exit_rate", 2023)] == pytest.approx(10.23)
    # Menos de 20 empleados: (10 + 20) de 100 empresas con clase de tamaño por empleo.
    assert by[("6231", "small_firm_share", 2022)] == pytest.approx(100 * 30 / 100)
    assert ("00", "establishments", 2023) in by  # total de la economía, para comparar
    # Idempotente: reconstruir no cambia nada.
    build(cfg, tmp_path / "processed")
    again = pq.read_table(tmp_path / "processed/market/naics_metrics/part-0.parquet").to_pylist()
    assert again == rows


def test_naics_coverage_of_market_series_is_complete(tmp_path):
    cfg = _cfg()
    adapters = _ingest_all(tmp_path, cfg)
    tables = naics.load_tables(cfg, REPO_ROOT)
    for src, adapter in adapters.items():
        # «00» es el total de la economía, no una industria: queda sin NAICS a propósito.
        ids = [i for i in adapter._series() if not i.split(":")[2] == "00"]
        links = naics.series_links({(src, i): "" for i in ids}, tables)  # solo el id
        assert all((src, i) in links for i in ids), src
    # Un código de NAICS 2017 que cambió en 2022 se pasa con la concordancia oficial.
    old = next(c for c, v in tables.to2022.items() if c not in tables.names and v)
    link = naics.series_links({("cbp", f"cbp:ESTAB:{old}"): ""}, tables)
    assert link and link[("cbp", f"cbp:ESTAB:{old}")][0][2] == "concordancia_naics"
    # Un código de una vintage más vieja (sin concordancia) se ancla a su ancestro vigente.
    for sid, expected in (("bds:FIRM:447", "44-45"), ("cbp:EMP:311822", "31182")):
        got = naics.series_links({(sid.split(":")[0], sid): ""}, tables)
        assert got[(sid.split(":")[0], sid)] == [(expected, 1.0, "ancestro_naics")], sid
