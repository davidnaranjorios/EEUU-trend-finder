"""Unitarios del adaptador Census: descubrimiento, frecuencia, filas de error, geo y key."""

from __future__ import annotations

import copy
import gzip
import logging
from datetime import UTC, date, datetime

import httpx

from src.config import load_config
from src.ingest.census import (
    get_chunks,
    parse_chunk,
    parse_time,
    plan_dataset,
)
from src.ingest.common import SourceIngestor, SourcePaths, latest_decisions
from tests.fakes_census import (
    FAKE_KEY,
    GEO_US,
    LONG_VARS,
    WIDE_VARS,
    bump,
    make_adapter,
)

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _cfg(**census):
    cfg = copy.deepcopy(load_config())
    cfg["download"]["groups"] = ["nacional"]
    cfg["sources"]["census"].update(lineage_aggregate_min_series=3, **census)
    return cfg


def _ing(tmp_path, **census):
    cfg = _cfg(**census)
    adapter = make_adapter(tmp_path, cfg)
    paths = SourcePaths(tmp_path / "processed", tmp_path / "raw", "census")
    return adapter, SourceIngestor(adapter, cfg, paths, now=lambda: NOW), paths


def _plan(vars_, geo=GEO_US):
    return plan_dataset(
        "timeseries/x/y", "/data/timeseries/x/y", "T", "2020", {"variables": vars_}, geo
    )


def test_parse_time_formats():
    assert parse_time("2024") == ("A", "YYYY", date(2024, 1, 1))
    assert parse_time("2024-09") == ("M", "YYYY-MM", date(2024, 9, 1))
    assert parse_time("2023-Q3") == ("Q", "YYYY-Qn", date(2023, 7, 1))
    assert parse_time("2020-W17")[0] is None
    assert parse_time("2024-13")[2] is None


def test_plan_classifies_variables():
    wide = _plan(WIDE_VARS)
    # TOT_S (error estándar) no es serie; TOT_F (flag, atributo de medida) no se pide; NATION
    # (predicateType nulo) se ignora; GRP_LABEL es etiqueta de GRP.
    assert wide.measures == ["PAY", "TOT", "YEAR"]
    assert wide.measures_excluded == 1
    assert wide.categoricals == ["GRP"]
    assert wide.labels == {"GRP": "GRP_LABEL"}
    assert wide.geo_for == "us:*" and wide.geo_level == "nacional"
    chunks = get_chunks(wide, 4)  # 2 fijas + time => 1 medida por bloque
    assert [c[-1] for c in chunks] == ["PAY", "TOT", "YEAR"]
    long = _plan(LONG_VARS)
    assert long.measures == [] and "cell_value" in long.categoricals


def test_geo_levels():
    state_only = _plan(LONG_VARS, {"fips": [{"name": "state"}, {"name": "county"}]})
    assert (state_only.geo_for, state_only.geo_level, state_only.geo_levels_omitted) == (
        "state:*",
        "regional",
        1,
    )
    countries = _plan(LONG_VARS, {"fips": [{"name": "genc standard countries and areas"}]})
    assert countries.geo_level == "internacional"
    none = _plan(LONG_VARS, {"default": [{"isDefault": "true"}]})
    assert none.geo_for is None and none.geo_level == "sin_geografia"
    no_time = _plan({k: v for k, v in LONG_VARS.items() if k != "time"})
    assert no_time.skip_reason == "sin_variable_time"


def test_long_format_dimensions_error_rows_and_seasonal():
    plan = _plan(LONG_VARS)
    header = [*plan.categoricals, "time", "us"]
    rows = []
    for t in ("2024-01", "2024-02", "2024-03"):
        for cat in ("A", "B"):
            for sa in ("yes", "no"):
                base = {"category_code": cat, "seasonally_adj": sa, "time_slot_id": "0"}
                ok = {**base, "cell_value": f"{len(rows)}.5", "data_type_code": "TOTAL"}
                err = {**base, "cell_value": "0.1", "data_type_code": "E_TOTAL"}
                rows.append([ok.get(h, "no") if h != "error_data" else "no" for h in header[:-2]])
                rows[-1] += [t, "1"]
                rows.append([err.get(h, "") if h != "error_data" else "yes" for h in header[:-2]])
                rows[-1] += [t, "1"]
    res = parse_chunk(plan, header, rows)
    assert res.counts["filas_error_excluidas"] == 12
    assert len(res.series) == 4  # categoría x ajuste; data_type y time_slot quedan constantes
    sid = "timeseries:x:y:cell_value:category_code=A:seasonally_adj=yes"
    s = res.series[sid]
    assert s.seasonal_adjustment == "SA" and s.formats == {"YYYY-MM"}
    assert len(s.obs) == 3 and all(isinstance(v, float) for v in s.obs.values())
    nsa = res.series[sid.replace("=yes", "=no")]
    assert nsa.seasonal_adjustment == "NSA" and nsa.title == s.title  # variantes: mismo título


def test_wide_format_labels_and_time_component():
    plan = _plan(WIDE_VARS)
    header = ["GRP", "GRP_LABEL", "PAY", "TOT", "YEAR", "time", "us"]
    rows = [
        [g, f"Group {g}", str(i), "(D)" if i == 0 else str(10 * i), y, y, "1"]
        for g in ("1", "2")
        for i, y in enumerate(("2001", "2002"))
    ]
    res = parse_chunk(plan, header, rows)
    assert res.counts["medidas_componente_tiempo"] == 1  # YEAR repite el año de `time`
    assert res.counts["valores_no_numericos"] == 2
    assert sorted(res.series) == [
        "timeseries:x:y:PAY:GRP=1",
        "timeseries:x:y:PAY:GRP=2",
        "timeseries:x:y:TOT:GRP=1",
        "timeseries:x:y:TOT:GRP=2",
    ]
    s = res.series["timeseries:x:y:TOT:GRP=1"]
    assert s.title == "T | Total widgets | Group code: Group 1 (1)"
    assert s.units == "Total widgets" and s.seasonal_adjustment is None


def test_crawl_counts_and_aggregated_lineage(tmp_path):
    adapter, ing, paths = _ing(tmp_path)
    out = ing.crawl(full=True, run_id="r1")
    assert out["units"] == 2  # el dataset no timeseries se ignora
    decisions = {d["series_id"]: d for d in latest_decisions(paths, "census")}
    agg = decisions["timeseries/demo/ancho/*"]
    assert agg["rule"] == "inactiva" and "n_series=6" in agg["detail"]  # 3 grupos x 2 medidas
    nac = [d for d in decisions.values() if d["group_name"] == "nacional"]
    assert len(nac) == 2  # A y B ajustadas; las NSA salen como variante
    assert adapter.stats["filas_error_excluidas"] > 0


def test_size_cap_skips_unit_and_records_reason(tmp_path):
    adapter, ing, paths = _ing(tmp_path, max_series_per_unit=1)
    out = ing.crawl(full=True, run_id="r1")
    assert out.get("series", 0) == 0
    skipped = adapter._load_skipped()
    # Sin datos recientes no hay sondeo: el tope se aplica tras la bajada completa.
    assert skipped["timeseries/demo/ancho"]["motivo"] == "excede_series:6"
    assert skipped["timeseries/demo/largo"]["motivo"].startswith("excede_series:")
    assert not skipped["timeseries/demo/largo"]["transient"]
    # Las unidades con fallo transitorio se reintentan (versión None); las de tope no.
    units = {u.unit_id: u for u in adapter.list_units()}
    assert units["timeseries/demo/largo"].catalog_version is not None


def test_probe_failure_is_transient(tmp_path):
    adapter, ing, paths = _ing(tmp_path, probe_max_retries=0)
    fake = adapter.fake
    original = fake.handler

    def failing(request):
        if request.url.path.endswith("/largo") and "from+2025" in str(request.url):
            raise httpx.ReadTimeout("slow", request=request)
        return original(request)

    adapter.probe_client._http._transport = httpx.MockTransport(failing)
    ing.crawl(full=True, run_id="r1")
    skipped = adapter._load_skipped()
    assert skipped["timeseries/demo/largo"]["transient"]
    units = {u.unit_id: u for u in adapter.list_units()}
    assert units["timeseries/demo/largo"].catalog_version is None


def test_key_never_in_logs_raw_or_errors(tmp_path, caplog):
    adapter, ing, paths = _ing(tmp_path)
    fake = adapter.fake
    original = fake.handler

    def echo_error(request):
        assert request.url.params.get("key") == FAKE_KEY  # la key sí viaja a la API
        if request.url.path.endswith("/ancho") and "from+2025" not in str(request.url):
            return httpx.Response(400, text=f"error: bad request {request.url}")
        return original(request)

    for c in (adapter.client, adapter.probe_client):
        c._http._transport = httpx.MockTransport(echo_error)
    with caplog.at_level(logging.DEBUG):
        ing.crawl(full=True, run_id="r1")
        ing.download()
    bump(adapter)
    assert FAKE_KEY not in caplog.text
    reason = adapter._load_skipped()["timeseries/demo/ancho"]["motivo"]
    assert reason.startswith("datos_http_400") and FAKE_KEY not in reason
    assert "***" in reason
    raws = list((tmp_path / "raw").rglob("*.json.gz"))
    assert raws
    for f in raws:
        assert FAKE_KEY not in gzip.open(f, "rt").read()
    for f in (tmp_path / "processed").rglob("*"):
        if f.is_file():
            assert FAKE_KEY.encode() not in f.read_bytes()
