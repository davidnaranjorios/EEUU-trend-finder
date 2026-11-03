"""Tests unitarios del adaptador BEA (sin red; fake en tests/fakes_bea.py)."""

from __future__ import annotations

import copy
import gzip
import logging
from datetime import UTC, date, datetime

import pytest

from src.config import load_config
from src.ingest import bea
from src.ingest.common import SourceIngestor, SourcePaths, latest_decisions
from src.ingest.http import ApiError
from tests import fakes_bea

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _cfg() -> dict:
    cfg = copy.deepcopy(load_config())
    cfg["download"]["groups"] = ["nacional"]
    cfg["sources"]["bea"]["lineage_aggregate_min_series"] = 3
    return cfg


@pytest.fixture
def setup(tmp_path):
    cfg = _cfg()
    adapter = fakes_bea.make_adapter(tmp_path, cfg)
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", "bea")
    return adapter, SourceIngestor(adapter, cfg, paths, now=lambda: NOW), paths


# --- funciones puras -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"TimePeriod": "2024"}, ("A", date(2024, 1, 1))),
        ({"TimePeriod": "2024Q3"}, ("Q", date(2024, 7, 1))),
        ({"TimePeriod": "2024M07"}, ("M", date(2024, 7, 1))),
        ({"TimePeriod": "2024Q5"}, None),
        ({"TimePeriod": "2024-25"}, None),
        ({"Year": "1997", "Quarter": "1997"}, ("A", date(1997, 1, 1))),
        ({"Year": "2024", "Quarter": "III"}, ("Q", date(2024, 7, 1))),
        ({"Year": "x"}, None),
    ],
)
def test_parse_period(row, expected):
    assert bea.parse_period(row) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,234.5", 1234.5), ("-6847", -6847.0), ("(NA)", None), ("(D)", None), ("...", None)],
)
def test_parse_value(raw, expected):
    assert bea.parse_value(raw) == expected


def test_seasonal_adjustment_and_geo():
    assert bea.seasonal_adjustment("QSA") == "SA"
    assert bea.seasonal_adjustment("QNSA") == "NSA"
    assert bea.seasonal_adjustment("A", "Table 1. Not seasonally adjusted") == "NSA"
    assert bea.seasonal_adjustment("A", "Tabla anual") is None
    assert bea.geo_of({"GeoFips": "00000", "GeoName": "United States"}) == ("nacional", None, None)
    assert bea.geo_of({"GeoFips": "01000", "GeoName": "Alabama"}) == (
        "regional",
        "01000",
        "Alabama",
    )
    assert bea.geo_of({"AreaOrCountry": "AllCountries"})[0] == "nacional"
    assert bea.geo_of({"AreaOrCountry": "Africa"}) == ("internacional", "Africa", None)
    assert bea.geo_of({"SeriesCode": "X"})[0] == "nacional"  # sin geografía → nacional


def test_api_error_inside_200():
    assert bea.api_error({"BEAAPI": {"Results": {"Error": {"APIErrorCode": "201"}}}})[0] == "201"
    assert bea.api_error({"BEAAPI": {"Error": {"APIErrorCode": "7"}}})[0] == "7"
    assert bea.api_error({"BEAAPI": {"Results": [{"Data": []}]}}) is None
    mne = {"BEAAPI": {"Results": {"Error": [{"number": "1", "error": "invalid value"}]}}}
    assert bea.api_error(mne) == ("1", "invalid value")


def test_scrub_removes_request_block():
    data = {"BEAAPI": {"Request": {"RequestParam": [{"ParameterValue": "k"}]}, "Results": {}}}
    assert "Request" not in bea.scrub(data)["BEAAPI"]
    assert "Request" in data["BEAAPI"]  # no muta la original


# --- adaptador sobre el fake -----------------------------------------------------------------


def test_discovery_is_generic(setup):
    adapter, _, _ = setup
    units = {u.unit_id: u for u in adapter.list_units()}
    assert set(units) == {"DSA:TA1", "DSA:TA2", "DSA:TA3", "DSR:RG1"}
    assert "META" in adapter.skipped  # sin parámetro de año, omitido con motivo
    plan = units["DSA:TA1"].extra["plan"]
    assert plan.unit_param == "TableName"  # nombre antes que id
    assert plan.params == {"Year": "X", "Frequency": "A,Q"}
    rplan = units["DSR:RG1"].extra["plan"]
    assert (rplan.geo_param, rplan.line_param) == ("GeoFips", "LineCode")


def test_catalog_rows_series_ids_titles_and_geo(setup):
    adapter, _, _ = setup
    units = {u.unit_id: u for u in adapter.list_units()}
    rows = {r["series_id"]: r for r in adapter.catalog_rows(units["DSA:TA1"])}
    assert len(rows) == 6
    q = rows["DSA:TA1:AAA.1:Q"]
    assert q["frequency"] == "Q" and q["seasonal_adjustment"] == "SA"
    assert q["observation_start"] == date(1990, 1, 1) and q["observation_end"] == date(2026, 4, 1)
    assert q["units"] == "Level (x10^6)" and q["geo_level"] == "nacional"
    assert q["last_updated"] == datetime(2026, 10, 29, tzinfo=UTC)
    # Dos líneas con la misma descripción no se confunden como variantes.
    assert rows["DSA:TA1:BBB.2:Q"]["title"] != rows["DSA:TA1:CCC.3:Q"]["title"]

    reg = {r["series_id"]: r for r in adapter.catalog_rows(units["DSR:RG1"])}
    assert reg["DSR:RG1:1:A"]["geo_level"] == "nacional"
    assert reg["DSR:RG1:1:01000:A"]["geo_level"] == "regional"
    assert reg["DSR:RG1:1:01000:A"]["title"].startswith("Línea 1, Alabama")
    # (NA) antes de 1990 en Alaska: la serie empieza en el primer valor numérico.
    assert reg["DSR:RG1:2:02000:A"]["observation_start"] == date(1990, 1, 1)
    assert len(reg) == 6  # 2 líneas x (nacional + 2 estados); el condado no se pide
    assert adapter.stats["geos_omitidas"] == 1
    calls = [c for c in adapter.fake.calls if c.get("GeoFips") == "STATE"]
    assert len(calls) == 2  # una solicitud estatal por línea


def test_error_inside_200_yields_no_rows_and_is_logged(setup, caplog):
    adapter, _, _ = setup
    units = {u.unit_id: u for u in adapter.list_units()}
    with caplog.at_level(logging.INFO):
        assert list(adapter.catalog_rows(units["DSA:TA3"])) == []
    assert adapter.stats["error_api_201"] == 1
    assert any("sin series de tiempo" in r.getMessage() for r in caplog.records)


def test_429_retries_with_retry_after(setup):
    adapter, _, _ = setup
    units = adapter.list_units()
    list(adapter.catalog_rows(units[0]))
    assert adapter.fake.sent_429
    assert any(s >= fakes_bea.RETRY_AFTER_S for s in adapter.fake.sleeps)
    assert adapter.client.n_retries == 1


def test_rate_limit_error_in_200_pauses_and_retries(tmp_path):
    adapter = fakes_bea.make_adapter(tmp_path, _cfg())
    fake = adapter.fake
    orig = fake._getdatasetlist
    state = {"n": 0}

    def flaky(q):
        state["n"] += 1
        if state["n"] == 1:
            return {"Error": {"APIErrorCode": "7", "APIErrorDescription": "limit"}}
        return orig(q)

    fake._getdatasetlist = flaky
    assert len(adapter.list_units()) == 4
    assert adapter.stats["limite_en_200"] == 1
    assert bea.WINDOW_S in fake.sleeps


def test_throttle_pauses_on_errors_and_bytes(tmp_path):
    adapter = fakes_bea.make_adapter(tmp_path, _cfg())
    for _ in range(bea.ERRORS_PER_MIN):
        adapter._note_error()
    before = len(adapter.fake.sleeps)
    adapter._throttle()
    assert len(adapter.fake.sleeps) == before + 1 and adapter.stats["pausas_por_limite"] == 1
    adapter._bytes.append((adapter.fake.now, bea.BYTES_PER_MIN))
    adapter._throttle()
    assert adapter.stats["pausas_por_limite"] == 2


def test_lineage_aggregate_and_groups(setup):
    adapter, ing, paths = setup
    ing.crawl(full=True, run_id="r1")
    dec = {d["series_id"]: d for d in latest_decisions(paths, "bea")}
    agg = dec["DSA:TA2/*"]
    assert agg["rule"] == "inactiva" and "n_series=3" in agg["detail"]
    assert dec["DSA:TA1:AAA.1:Q"]["group_name"] == "nacional"
    assert dec["DSA:TA1:AAA.1:A"]["rule"] == "variante"
    assert dec["DSR:RG1:1:01000:A"]["group_name"] == "regional"


def test_one_request_per_unit_per_run(setup):
    adapter, ing, _ = setup
    ing.crawl(full=True, run_id="r1")
    n = len([c for c in adapter.fake.calls if c["method"] == "GetData"])
    ing.download()  # misma corrida: valores desde la caché del crawl
    assert len([c for c in adapter.fake.calls if c["method"] == "GetData"]) == n


def test_key_never_in_raw_or_logs(setup, caplog, tmp_path):
    adapter, ing, _ = setup
    with caplog.at_level(logging.DEBUG):
        ing.crawl(full=True, run_id="r1")
        ing.download()
        adapter.close()
    raws = list((tmp_path / "raw").rglob("*.json.gz"))
    assert raws
    for f in raws:
        text = gzip.open(f, "rt", encoding="utf-8").read()
        assert fakes_bea.FAKE_KEY not in text and "USERID" not in text
    for rec in caplog.records:
        assert fakes_bea.FAKE_KEY not in rec.getMessage()
        assert fakes_bea.FAKE_KEY not in str(getattr(rec, "fields", ""))


def test_http_error_message_is_redacted(tmp_path):
    import httpx

    def handler(request):
        return httpx.Response(400, text=f"bad request {request.url}")

    adapter = bea.build_adapter(
        _cfg()["sources"]["bea"],
        None,
        fakes_bea.FAKE_KEY,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    with pytest.raises(ApiError) as exc:
        adapter.list_units()
    assert fakes_bea.FAKE_KEY not in str(exc.value)
