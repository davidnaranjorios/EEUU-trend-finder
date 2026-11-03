"""Tests propios del adaptador BLS (archivos masivos), sin red."""

from __future__ import annotations

import copy
import logging
from datetime import UTC, date, datetime

import pytest

from src.config import load_config
from src.ingest import bls
from src.ingest.common import SourceIngestor, SourcePaths, latest_decisions
from tests import fakes_bls

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["download"]["groups"] = ["nacional"]
    c["sources"]["bls"]["lineage_aggregate_min_series"] = 3
    return c


@pytest.fixture
def setup(cfg, tmp_path):
    adapter = fakes_bls.make_adapter(tmp_path, cfg)
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", "bls")
    return adapter, SourceIngestor(adapter, cfg, paths, now=lambda: NOW), paths


# --- funciones puras ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("begin", "end", "freq"),
    [
        ("M01", "M09", "M"),
        ("M01", "M13", "M"),  # el promedio anual final no define frecuencia
        ("Q01", "Q03", "Q"),
        ("Q01", "Q05", "Q"),
        ("A01", "A01", "A"),
        ("M13", "M13", "A"),  # serie publicada solo como promedio anual
        ("S01", "S02", "S"),
        ("X01", "X02", None),
    ],
)
def test_series_frequency(begin, end, freq):
    assert bls.series_frequency(begin, end) == freq


def test_period_dates():
    assert bls.period_date(2026, "Q03", "Q", end=True) == date(2026, 7, 1)
    assert bls.period_date(2025, "M13", "M", end=True) == date(2025, 12, 1)
    assert bls.period_date(2025, "Q05", "Q", end=True) == date(2025, 10, 1)
    assert bls.period_date(2025, "A01", "A", end=False) == date(2025, 1, 1)
    assert bls.obs_date(2025, "M13", "M") is None
    assert bls.obs_date(2025, "Q05", "Q") is None
    assert bls.obs_date(2025, "M13", "A") == date(2025, 1, 1)
    assert bls.obs_date(2025, "Q02", "M") is None
    assert bls.obs_date(2025, "S01", "M") is None


def test_code_table_with_and_without_header():
    with_header = [["area_type_code", "area_code", "area_text"], ["A", "X1", "Uno"]]
    assert bls.parse_code_table(with_header, "area_code") == {"X1": "Uno"}
    headerless = [["0000", "U.S. City Average"], ["0100", "Northeast Region"]]
    assert bls.parse_code_table(headerless, "area_code")["0000"] == "U.S. City Average"


SM_MAPS = {
    "state_code": {"00": "All States", "01": "A"},
    "area_code": {"0": "Statewide", "1": "B"},
}
COUNTRY_MAPS = {"country_code": {"CA": "Canada", "US": "UNITED STATES"}}


@pytest.mark.parametrize(
    ("codes", "maps", "level"),
    [
        ({}, {}, "nacional"),
        ({"area_code": "0000"}, {"area_code": {"0000": "U.S. city average", "1": "X"}}, "nacional"),
        ({"area_code": "1"}, {"area_code": {"0000": "U.S. city average", "1": "X"}}, "regional"),
        # Dimensión de una sola fila: no informa.
        (
            {"state_code": "00", "area_code": "0"},
            {"state_code": {"00": "Total US", "01": "Alabama"}, "area_code": {"0": "All areas"}},
            "nacional",
        ),
        # "Statewide" es neutro solo si otra dimensión es nacional.
        (
            {"state_code": "00", "area_code": "0"},
            SM_MAPS,
            "nacional",
        ),
        (
            {"state_code": "01", "area_code": "0"},
            SM_MAPS,
            "regional",
        ),
        ({"area_code": "9"}, {"area_code": {"9": "National City city, CA", "1": "X"}}, "regional"),
        ({"area_code": "9"}, {"area_code": {"9": "U.S. Virgin Islands", "1": "X"}}, "regional"),
        ({"country_code": "CA"}, COUNTRY_MAPS, "internacional"),
        ({"area_code": "9"}, {}, "regional"),  # sin tabla: no verificable
    ],
)  # fmt: skip
def test_geo_classify(codes, maps, level):
    assert bls.geo_classify(codes, maps)[0] == level


def test_clean_title_drops_seasonal_text():
    seasonal = {"S": "Seasonally Adjusted", "U": "Not Seasonally Adjusted"}
    a = bls.clean_title("All employees, total nonfarm, seasonally adjusted", seasonal)
    b = bls.clean_title("All employees, total nonfarm, not seasonally adjusted", seasonal)
    c = bls.clean_title("Unemployment Rate: Alabama (S)", seasonal)
    assert a == b == "All employees, total nonfarm"
    assert c == "Unemployment Rate: Alabama"
    assert bls.clean_title("Seasonally Adjusted", seasonal) == "Seasonally Adjusted"


# --- adaptador contra el servidor falso -----------------------------------------------------------


def test_units_skip_non_survey_dirs_and_use_etag(setup):
    adapter, _, _ = setup
    units = {u.unit_id: u for u in adapter.list_units()}
    assert set(units) == {"cu", "jt", "zz"}  # compressed/ no tiene .series
    assert units["cu"].catalog_version == adapter.fake.etag("cu/cu.series")
    assert units["cu"].extra["name"].startswith("Consumer Price Index")
    assert units["zz"].extra["name"] == "zz"  # sin nombre en el resumen: el código


def test_catalog_rows_parsing_and_malformed(setup, caplog):
    adapter, _, _ = setup
    units = {u.unit_id: u for u in adapter.list_units()}
    with caplog.at_level(logging.INFO):
        rows = {r["series_id"]: r for r in adapter.catalog_rows(units["cu"])}
    assert "CUBROKEN" not in rows
    rec = [r for r in caplog.records if getattr(r, "fields", {}).get("unit") == "cu"]
    assert rec and rec[-1].fields["filas_malformadas"] == 1
    sa = rows["CUSR0000SA0"]
    assert sa["title"] == rows["CUUR0000SA0"]["title"] == "All items in U.S. city average"
    assert (sa["frequency"], sa["frequency_raw"], sa["seasonal_adjustment"]) == ("M", "M09", "SA")
    assert sa["observation_start"] == date(2000, 1, 1)
    assert sa["observation_end"] == date(2026, 9, 1)
    assert (sa["geo_level"], sa["geo_detail"]) == ("nacional", "area_code=0000")
    assert sa["copyrighted"] is False and sa["discontinued"] is False
    assert rows["CUUR0100SA0"]["geo_level"] == "regional"
    assert rows["CUUR0000SA0S1"]["frequency"] is None  # semestral
    jt = {r["series_id"]: r for r in adapter.catalog_rows(units["jt"])}
    # Sin series_title: título desde las tablas de códigos, en orden de cabecera.
    assert jt["JTS00Q"]["title"] == "Total nonfarm, Total US, All areas, Job openings, Level"
    assert jt["JTS00Q"]["frequency"] == "Q"
    assert jt["JTS00Q"]["observation_end"] == date(2026, 7, 1)
    assert jt["JTS00Q"]["geo_level"] == "nacional"
    assert jt["JTS06Q"]["geo_level"] == "regional"


def test_funnel_and_download(setup):
    adapter, ing, paths = setup
    ing.crawl(full=True, run_id="r1")
    dec = {d["series_id"]: d for d in latest_decisions(paths, "bls")}
    assert dec["CUSR0000SA0"]["group_name"] == "nacional"
    assert dec["CUUR0000SA0"]["rule"] == "variante"  # misma variable NSA
    assert dec["CUUR0100SA0"]["group_name"] == "regional"
    assert dec["CUUR0000SA0S1"]["rule"] == "frecuencia_no_soportada"
    assert dec["JTS00Q"]["group_name"] == "nacional"
    assert dec["zz/*"]["rule"] == "inactiva"  # encuesta entera inactiva: fila agregada
    assert "version=" + adapter.fake.etag("zz/zz.series") in dec["zz/*"]["detail"]

    adapter.fake.requests.clear()
    res = ing.download()
    assert res["rows_inserted"] > 0 and res["units_pending"] == 0
    got = [r.url.path for r in adapter.fake.requests if r.method == "GET"]
    # El archivo grande ya cubre las series: el subconjunto reciente no se baja.
    assert any(p.endswith("cu.data.1.AllItems") for p in got)
    assert not any(p.endswith("cu.data.0.Current") for p in got)


def test_fetch_values_filters_periods_and_values(setup):
    adapter, ing, _ = setup
    ing.crawl(full=True, run_id="r1")
    units = {u.unit_id: u for u in adapter.list_units()}
    rows = list(adapter.fetch_values(units["cu"], {"CUSR0000SA0", "CUUR0100SA0"}))
    assert {r["series_id"] for r in rows} == {"CUSR0000SA0", "CUUR0100SA0"}
    assert all(r["date"].day == 1 for r in rows)
    reg = [r for r in rows if r["series_id"] == "CUUR0100SA0"]
    assert sum(r["value"] is None for r in reg) == 1  # "-" → None
    assert all(r["frequency"] == "M" and r["seasonal_adjustment"] for r in rows)
    jt = list(adapter.fetch_values(units["jt"], {"JTS00Q"}))
    assert all(r["date"].month in (1, 4, 7, 10) for r in jt)  # Q05 descartado
    assert len(jt) == 26 * 4 - 1  # 2001Q1–2026Q3


def test_user_agent_sent_and_never_logged(setup, caplog):
    adapter, ing, _ = setup
    with caplog.at_level(logging.DEBUG):
        ing.crawl(full=True, run_id="r1")
        ing.download()
    assert adapter.fake.requests
    assert all(r.headers["user-agent"] == fakes_bls.USER_AGENT for r in adapter.fake.requests)
    logged = "\n".join(r.getMessage() + repr(getattr(r, "fields", "")) for r in caplog.records)
    assert fakes_bls.USER_AGENT not in logged
    assert "example.org" not in logged


def test_user_agent_missing_env_is_clear_error(cfg, monkeypatch):
    name = cfg["sources"]["user_agent_env"]
    monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match=name):
        bls.user_agent_from_env(cfg)
    monkeypatch.setenv(name, "Alguien x@example.org")
    assert bls.user_agent_from_env(cfg) == "Alguien x@example.org"


def test_repair_header_glued_first_row_and_empty_columns():
    glued = ["series_id", "seasonal", "begin_year", "end_periodXX1 ", "U", "1990"]
    header, rows = bls.repair_header(glued, ["XX2", "U", "1991", "Q04"])
    assert header == ["series_id", "seasonal", "begin_year", "end_period"]
    assert rows == [["XX1 ", "U", "1990"], ["XX2", "U", "1991", "Q04"]]
    extra = ["series_id", "a_code", "", "end_period"]
    assert bls.repair_header(extra, ["X", "1", "M01"]) == (
        ["series_id", "a_code", "end_period"],
        [["X", "1", "M01"]],
    )
    ok = ["series_id", "end_period"]
    assert bls.repair_header(ok, ["X", "M01"]) == (ok, [["X", "M01"]])
