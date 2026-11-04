"""Fase 1: ingesta FRED contra un servidor falso (sin red)."""

import copy
import hashlib
import json
import logging
import random
import shutil
from datetime import UTC, date, datetime, timedelta

import pyarrow as pa
import pytest

from src import eligibility
from src.catalog import connect
from src.config import load_config
from src.ingest.fred import (
    EXIT_PENDING,
    FETCH_STATE,
    FRED_CATALOG,
    SERIES_ATTRS,
    SOURCE,
    VINTAGE_SAMPLE,
    FredIngestor,
    Paths,
    collapse_vintages,
    stratified_sample,
)
from src.ingest.http import ApiClient, ApiError, RateLimiter
from src.ingest.store import read_table, recover, upsert
from src.log import run
from src.schemas import SERIES, SERIES_META, validate
from tests.fake_fred import FakeFred, make_info

API_KEY = "k" * 32
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class Now:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def fake():
    f = FakeFred()
    f.add_category(1, "Precios", 0)
    f.add_category(3, "Consumo", 1)
    f.add_category(2, "Empleo", 0)
    f.add_series(3, make_info("AAA"), n_obs=320)
    f.add_series(2, make_info("AAA"))  # misma serie en dos categorías
    f.add_series(3, make_info("BBB", notes="Copyright, 2026, Fuente privada."), n_obs=320)
    f.add_series(2, make_info("CCC", title="Vieja (DISCONTINUED)"), n_obs=320)
    f.add_series(2, make_info("DDD", frequency_short="SA"), n_obs=10)
    f.add_series(2, make_info("EEE", start="2025-01-01"), n_obs=20)
    f.add_series(2, make_info("FFF", notes=None), n_obs=320)
    for sid in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"):
        f.set_meta(sid, {"nation", "usa"}, release=10 if sid < "D" else 20, agency=f"Ag{sid < 'C'}")
    return f


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["fred"].update(page_limit_series=2, page_limit_observations=50, obs_batch_size=2, n_buckets=4)
    c["download"]["v2_page_limit"] = 250
    return c


@pytest.fixture
def make(fake, cfg, tmp_path):
    paths = Paths(tmp_path / "processed", tmp_path / "raw")
    clock = FakeClock()
    now = Now(T0)

    def _client(base: str, auth: dict, headers: dict | None, raw: bool) -> ApiClient:
        return ApiClient(
            base,
            auth,
            headers=headers,
            requests_per_minute=6000,
            max_retries=3,
            timeout_s=5,
            backoff_base_s=1.0,
            raw_dir=paths.raw / SOURCE if raw else None,
            transport=fake.transport(),
            clock=clock,
            sleep=clock.sleep,
            today=lambda: now().date(),
            rng=random.Random(0),
        )

    def _make() -> FredIngestor:
        client = _client("https://api.stlouisfed.org/fred/", {"api_key": API_KEY}, None, True)
        v2 = _client(
            "https://api.stlouisfed.org/fred/v2/",
            {},
            {"Authorization": f"Bearer {API_KEY}"},
            False,
        )
        return FredIngestor(client, cfg, paths, now=now, v2=v2)

    _make.paths = paths
    _make.now = now
    _make.clock = clock
    _make.cfg = cfg
    return _make


def run_funnel(make, run_id="run-1", today=None):
    catalog, attrs = eligibility.load_inputs(make.paths)
    decisions = eligibility.classify(catalog, attrs, make.cfg, today or make.now().date())
    eligibility.write_lineage(decisions, run_id, make.now(), make.paths, n_buckets=4)
    return {d["series_id"]: d for d in decisions}


def prepare(make):
    """Catálogo + metadatos + embudo: deja las series objetivo listas para descargar."""
    ing = make()
    ing.crawl()
    ing.crawl_meta()
    run_funnel(make)
    return ing


def series_rows(paths):
    return read_table(SERIES, paths.series, source=SOURCE)


def checksum(table: pa.Table) -> str:
    rows = sorted(tuple(map(str, r.values())) for r in table.to_pylist())
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


# --- crawl -------------------------------------------------------------------------------------


def test_crawl_builds_catalog_and_meta(make):
    stats = make().crawl()
    assert stats["series"] == 6
    meta = {
        r["series_id"]: r
        for r in read_table(SERIES_META, make.paths.series_meta, source=SOURCE).to_pylist()
    }
    assert set(meta) == {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF"}
    assert meta["AAA"]["category_path"] == "Empleo;Precios > Consumo"
    assert meta["BBB"]["copyrighted"] and not meta["AAA"]["copyrighted"]
    assert meta["CCC"]["discontinued"]
    assert meta["AAA"]["last_updated"] == datetime(2026, 10, 15, 12, 50, 2, tzinfo=UTC)
    assert stats["notes_missing"] == 1  # FFF: el crawl lo reporta, no lo oculta


def test_pagination_category_series(make, fake):
    make().crawl()
    # Empleo tiene 5 series y page_limit_series=2: 3 páginas, ninguna serie perdida.
    assert fake.count("category/series", category_id="2") == 3
    catalog = read_table(FRED_CATALOG, make.paths.catalog, source=SOURCE)
    assert catalog.num_rows == 6


# --- observations ------------------------------------------------------------------------------


def test_crawl_meta_geo_and_releases(make, fake):
    ing = make()
    ing.crawl()
    stats = ing.crawl_meta()
    attrs = {
        r["series_id"]: r
        for r in read_table(SERIES_ATTRS, make.paths.series_attrs, source=SOURCE).to_pylist()
    }
    assert attrs["AAA"]["geo_types"] == ["nation"] and attrs["AAA"]["is_usa"]
    assert attrs["AAA"]["release_ids"] == [10] and attrs["AAA"]["agencies"] == ["AgTrue"]
    assert stats["releases"] == 2 and stats["series_with_release"] == 6
    # tags/series informa un count inflado: se pagina hasta la página corta, sin fallar.
    assert stats["geo_tags"]["usa"] == 6


def test_observations_require_funnel(make):
    ing = make()
    ing.crawl()
    with pytest.raises(RuntimeError, match="embudo"):
        ing.observations()


def test_pagination_observations(make, fake):
    ing = prepare(make)
    stats = ing.observations()
    assert stats["targets"] == 3 and stats["fetched"] == 3
    # 320 observaciones con page_limit_observations=50: 7 páginas por serie.
    assert fake.count("series/observations", series_id="AAA") == 7
    rows = series_rows(make.paths)
    assert rows.num_rows == 3 * 320
    validate(rows, SERIES)


def test_current_values_have_no_vintage(make):
    ing = prepare(make)
    ing.observations()
    rows = series_rows(make.paths).to_pylist()
    assert {r["vintage_date"] for r in rows} == {None}  # "no aplica": no se inventa una fecha
    row = next(r for r in rows if r["series_id"] == "AAA" and r["date"] == date(2000, 1, 1))
    assert row["frequency"] == "M" and row["units"] == "Index" and row["value"] == 100.0


def test_fred_idempotent(make):
    ing = prepare(make)
    ing.observations()
    first = series_rows(make.paths)
    meta_first = read_table(SERIES_META, make.paths.series_meta, source=SOURCE)

    # Segunda corrida normal: nada cambió, nada se pide de nuevo.
    again = make()
    again.crawl()
    assert again.observations()["fetched"] == 0

    # Corrida forzada desde cero (sin estado): se vuelve a descargar todo y no duplica.
    shutil.rmtree(make.paths.fetch_state)
    stats = make().observations()
    assert stats["fetched"] == 3
    assert stats["rows_inserted"] == 0 and stats["rows_unchanged"] == 3 * 320
    second = series_rows(make.paths)
    assert second.num_rows == first.num_rows
    assert checksum(second) == checksum(first)
    assert checksum(read_table(SERIES_META, make.paths.series_meta, source=SOURCE)) == checksum(
        meta_first
    )


def test_only_target_groups_fetched(make, fake):
    ing = prepare(make)
    ing.observations()
    fetched = {p["series_id"] for e, p in fake.requests if e == "series/observations"}
    # CCC descontinuada, DDD frecuencia no soportada, EEE historia corta (grupo "nuevas").
    assert fetched == {"AAA", "BBB", "FFF"}


def test_reference_series_are_downloaded_outside_target_groups(make, fake, cfg):
    # Una serie de referencia metodológica (ADR 0009) que el embudo deja fuera (EEE: historia
    # corta) igual se descarga: la ficha del fenómeno la necesita.
    cfg["context"]["reference_series"] = {
        "deflator": "EEE",
        "population": "AAA",
        "recessions": "BBB",
    }
    ing = prepare(make)
    ing.observations()
    fetched = {p["series_id"] for e, p in fake.requests if e == "series/observations"}
    assert fetched == {"AAA", "BBB", "FFF", "EEE"}


def test_observations_max_series_reports_pending(make):
    ing = prepare(make)
    assert ing.observations(max_series=2)["pending"] == 1
    assert make().observations(max_series=2)["pending"] == 0


def test_api_errors_per_series_are_counted(make, fake):
    fake.missing.add("BBB")
    ing = prepare(make)
    stats = ing.observations()
    assert stats["series_errors"] == 1 and stats["fetched"] == 2
    # Sin estado guardado para BBB: se reintentará en la próxima corrida.
    state = read_table(FETCH_STATE, make.paths.fetch_state, source=SOURCE).to_pylist()
    assert "BBB" not in {r["series_id"] for r in state}


# --- download (API v2) -------------------------------------------------------------------------


def test_v2_download_cursor_and_filter(make, fake):
    ing = prepare(make)
    stats = ing.download()
    # Release 10: AAA, BBB, CCC (960 obs vigentes) con páginas de 250 -> 4 páginas.
    # Release 20: DDD, EEE, FFF (350 obs) -> 2 páginas. Solo se escriben AAA, BBB y FFF.
    assert stats["releases"] == 2 and stats["pending_releases"] == 0
    assert stats["pages"] == 6
    assert fake.count("v2/release/observations") == 6
    rows = series_rows(make.paths)
    assert sorted(set(rows.column("series_id").to_pylist())) == ["AAA", "BBB", "FFF"]
    assert rows.num_rows == 3 * 320
    assert set(rows.column("vintage_date").to_pylist()) == {None}
    validate(rows, SERIES)


def test_v2_download_same_as_v1(make):
    ing = prepare(make)
    ing.download()
    via_v2 = checksum(series_rows(make.paths))
    shutil.rmtree(make.paths.series)
    shutil.rmtree(make.paths.fetch_state)
    make().observations()
    assert checksum(series_rows(make.paths)) == via_v2


def test_v2_download_resumes_by_release(make, fake):
    ing = prepare(make)
    first = ing.download(max_releases=1)
    assert first["releases_done_now"] == 1 and first["pending_releases"] == 1
    calls = fake.count("v2/release/observations")
    second = make().download()
    assert second["releases_done_now"] == 1 and second["pending_releases"] == 0
    assert fake.count("v2/release/observations", release_id="10") == calls  # no se repite
    assert series_rows(make.paths).num_rows == 3 * 320
    assert make().download()["releases_done_now"] == 0


def test_v2_download_idempotent(make):
    ing = prepare(make)
    ing.download()
    first = series_rows(make.paths)
    make.paths.cursors.unlink()  # olvidar el progreso: se rebaja todo
    stats = make().download()
    assert stats["rows_written"] == 3 * 320
    assert checksum(series_rows(make.paths)) == checksum(first)


def test_cli_exit_code_when_pending(make, monkeypatch):
    from src.ingest import fred as fred_mod

    prepare(make)
    monkeypatch.setenv("FRED_API_KEY", API_KEY)
    monkeypatch.setattr(fred_mod.FredIngestor, "from_config", classmethod(lambda c, *a: make()))
    monkeypatch.setattr(fred_mod, "load_config", lambda: make.cfg)
    monkeypatch.setitem(make.cfg["logging"], "runs_file", str(make.paths.processed / "runs.jsonl"))
    assert fred_mod.main(["download", "--max-releases", "1"]) == EXIT_PENDING
    assert fred_mod.main(["download", "--max-releases", "1"]) == 0


# --- vintages (muestra ALFRED) -----------------------------------------------------------------


def test_vintage_sample_is_stratified_and_fixed(make):
    ing = prepare(make)
    make.cfg["vintages"]["sample_size"] = 2
    stats = ing.vintages()
    sample = read_table(VINTAGE_SAMPLE, make.paths.vintage_sample, source=SOURCE).to_pylist()
    # Dos estratos (M|AgTrue: AAA, BBB; M|AgFalse: FFF): al menos una serie de cada uno.
    assert {r["stratum"] for r in sample} == {"M|AgTrue", "M|AgFalse"}
    assert stats["sample"] == len(sample) == 2
    make.cfg["vintages"]["seed"] += 1  # otra semilla no vuelve a sortear: la muestra es fija
    assert make().vintages()["sample"] == 2
    again = read_table(VINTAGE_SAMPLE, make.paths.vintage_sample, source=SOURCE).to_pylist()
    assert again == sample


def test_vintage_date_is_publication_date(make):
    ing = prepare(make)
    ing.vintages(ids=["AAA"])
    row = next(
        r
        for r in series_rows(make.paths).to_pylist()
        if r["series_id"] == "AAA" and r["date"] == date(2000, 1, 1)
    )
    assert row["vintage_date"] == date(2000, 2, 15)  # realtime_start, no la fecha de hoy


def test_vintages_split_into_windows_over_cap(make, fake):
    fake.max_vintages = 100
    make.cfg["vintages"]["max_vintage_dates_per_request"] = 100
    make.cfg["vintages"]["sample_size"] = 0
    fake.revise("AAA", "2000-01-01", "50.0", on="2001-01-01")
    ing = prepare(make)
    stats = ing.vintages(ids=["AAA"])
    assert stats["fetched"] == 1 and stats["series_errors"] == 0
    # 321 vintages con tope 100: 4 ventanas de series/observations, sin pedir más del tope.
    windows = [p for e, p in fake.requests if e == "series/observations" and p["offset"] == "0"]
    assert len(windows) == 4
    aaa = [r for r in series_rows(make.paths).to_pylist() if r["series_id"] == "AAA"]
    assert len(aaa) == 321  # 320 publicaciones originales + la revisión, sin duplicados
    first = sorted(
        (r for r in aaa if r["date"] == date(2000, 1, 1)), key=lambda r: r["vintage_date"]
    )
    assert [(r["vintage_date"], r["value"]) for r in first] == [
        (date(2000, 2, 15), 100.0),
        (date(2001, 1, 1), 50.0),
    ]


def test_vintages_ids_extend_sample(make):
    make.cfg["vintages"]["sample_size"] = 0
    ing = prepare(make)
    stats = ing.vintages(ids=["EEE", "NOPE"])
    assert stats["extra_ids"] == 2 and stats["unknown_ids"] == 1 and stats["fetched"] == 1


def test_collapse_vintages_drops_window_repeats():
    obs = [
        {"date": "2000-01-01", "realtime_start": "2000-02-15", "value": "1"},
        {"date": "2000-01-01", "realtime_start": "2005-01-01", "value": "1"},  # borde de tramo
        {"date": "2000-01-01", "realtime_start": "2006-01-01", "value": "2"},
    ]
    assert [o["realtime_start"] for o in collapse_vintages(obs)] == ["2000-02-15", "2006-01-01"]


def test_stratified_sample_deterministic():
    strata = {"A": [f"a{i}" for i in range(90)], "B": [f"b{i}" for i in range(10)], "C": ["c"]}
    s1 = stratified_sample(strata, 20, seed=7)
    assert s1 == stratified_sample(strata, 20, seed=7)
    assert {k: len(v) for k, v in s1.items()} == {"A": 18, "B": 2, "C": 1}
    assert s1 != stratified_sample(strata, 20, seed=8)


# --- update / incremental ----------------------------------------------------------------------


def test_fred_incremental(make, fake):
    ing = prepare(make)
    ing.update()  # primera vez: crawl completo
    before = len(fake.requests)

    fake.revise("AAA", "2026-08-01", "999.0", on="2026-10-25")
    fake.infos["AAA"]["last_updated"] = "2026-10-25 07:50:02-05"
    fake.updates = ["AAA"]
    make.now.t = T0 + timedelta(days=7)

    stats = make().update()
    assert stats["mode"] == "updates"
    new_requests = fake.requests[before:]
    fetched = {p["series_id"] for e, p in new_requests if e == "series/observations"}
    assert fetched == {"AAA"}
    assert not any(e.startswith("category/") for e, _ in new_requests)


def test_revision_replaces_current_value(make, fake):
    ing = prepare(make)
    ing.update()
    fake.revise("AAA", "2026-08-01", "999.0", on="2026-10-25")
    fake.infos["AAA"]["last_updated"] = "2026-10-25 07:50:02-05"
    fake.updates = ["AAA"]
    fake.today = "2026-10-27"
    make.now.t = T0 + timedelta(days=7)
    stats = make().update()
    assert stats["observations"]["rows_replaced"] == 1
    con = connect(make.paths.processed)
    rows = con.execute(
        "SELECT vintage_date, value FROM series WHERE series_id='AAA' AND date='2026-08-01'"
    ).fetchall()
    assert rows == [(None, 999.0)]


def test_revision_appends_vintage(make, fake):
    ing = prepare(make)
    ing.vintages(ids=["AAA"])
    fake.revise("AAA", "2026-08-01", "999.0", on="2026-10-25")
    fake.infos["AAA"]["last_updated"] = "2026-10-25 07:50:02-05"
    fake.updates = ["AAA"]
    make.now.t = T0 + timedelta(days=7)
    make().update()
    make().vintages(ids=["AAA"])

    con = connect(make.paths.processed)
    rows = con.execute(
        "SELECT vintage_date, value FROM series WHERE series_id='AAA' AND date='2026-08-01' "
        "AND vintage_date IS NOT NULL ORDER BY vintage_date"
    ).fetchall()
    assert rows == [(date(2026, 9, 15), 259.5), (date(2026, 10, 25), 999.0)]
    # Vista "tal como se conocía" en una fecha: sin información del futuro.
    as_of = con.execute(
        "SELECT value FROM series WHERE series_id='AAA' AND date='2026-08-01' "
        "AND vintage_date <= DATE '2026-10-01' ORDER BY vintage_date DESC LIMIT 1"
    ).fetchone()
    assert as_of == (259.5,)


def test_update_tolerates_duplicate_series_in_updates(make, fake):
    # series/updates pagina por last_updated: una serie actualizada a mitad del recorrido puede
    # salir en dos páginas. Antes eso rompía la validación (claves duplicadas en fred_catalog).
    prepare(make).update()
    fake.updates = ["AAA", "AAA"]
    make.now.t = T0 + timedelta(days=7)
    stats = make().update()
    assert stats["mode"] == "updates" and stats["updated_series"] == 1


def test_dedupe_updates_keeps_newest():
    from src.ingest.fred import dedupe_updates

    infos = [
        {"id": "A", "last_updated": "2026-10-01 07:00:00-05"},
        {"id": "B", "last_updated": "2026-10-02 07:00:00-05"},
        {"id": "A", "last_updated": "2026-10-03 07:00:00-05"},
    ]
    out = dedupe_updates(infos)
    assert [i["id"] for i in out] == ["A", "B"]
    assert out[0]["last_updated"].startswith("2026-10-03")


def test_update_falls_back_to_crawl_after_gap(make, fake):
    prepare(make).update()
    make.now.t = T0 + timedelta(days=20)  # más que la ventana de series/updates
    stats = make().update()
    assert stats["mode"] == "crawl_fallback"


def test_retries_exhausted_abort_and_cursor_not_advanced(make, fake, tmp_path):
    prepare(make).update()
    cursors_before = json.loads(make.paths.cursors.read_text())
    fake.updates = ["AAA"]
    make.now.t = T0 + timedelta(days=7)
    fake.always_fail = 503
    runs_file = tmp_path / "runs.jsonl"
    with pytest.raises(ApiError, match="reintentos agotados"), run([SOURCE], runs_file):
        make().update()
    assert json.loads(make.paths.cursors.read_text()) == cursors_before
    assert json.loads(runs_file.read_text())["status"] == "failed"


# --- HTTP --------------------------------------------------------------------------------------


def test_backoff_on_429(make, fake):
    fake.fail_queue = [429, 429]
    ing = make()
    data = ing._get("category/children", {"category_id": 0})
    assert [c["id"] for c in data["categories"]] == [1, 2]
    assert ing.client.n_retries == 2
    backoffs = [s for s in make.clock.sleeps if s >= 1.0]
    assert len(backoffs) == 2 and backoffs[1] > backoffs[0]


def test_non_retryable_error_raises_immediately(make, fake):
    fake.fail_queue = [400]
    with pytest.raises(ApiError) as exc:
        make()._get("category/children", {"category_id": 0})
    assert exc.value.status == 400
    assert len(fake.requests) == 1


def test_rate_limiter_spacing():
    clock = FakeClock()
    limiter = RateLimiter(60, clock, clock.sleep)
    for _ in range(5):
        limiter.wait()
    assert clock.t == pytest.approx(4.0)  # 1 s entre solicitudes a 60/min


def test_api_key_never_written(make, fake, caplog):
    caplog.set_level(logging.DEBUG)
    ing = prepare(make)
    ing.observations()
    ing.download()
    # v2 la lleva en el header (no en la URL), y nunca llega a raw ni a los logs.
    v2_headers = [h for (e, _), h in zip(fake.requests, fake.headers, strict=True) if "v2/" in e]
    assert v2_headers and all(h["authorization"] == f"Bearer {API_KEY}" for h in v2_headers)
    assert not any(API_KEY in str(p) for e, p in fake.requests if e.startswith("v2/"))
    for path in make.paths.raw.rglob("*.json.gz"):
        import gzip

        assert API_KEY not in gzip.open(path, "rt").read()
    assert API_KEY not in caplog.text


# --- consulta DuckDB y almacén -------------------------------------------------------------


def test_duckdb_category_query(make):
    ing = prepare(make)
    ing.observations()
    con = connect(make.paths.processed)
    rows = con.execute(
        """
        SELECT m.series_id, count(s.date) AS n_obs
        FROM series_meta m LEFT JOIN series s USING (source, series_id)
        WHERE list_contains(string_split(m.category_path, ';'), 'Precios > Consumo')
        GROUP BY m.series_id ORDER BY m.series_id
        """
    ).fetchall()
    assert rows == [("AAA", 320), ("BBB", 320)]


def test_store_conflict_replaces(tmp_path):
    def row(value):
        return {
            "source": "x",
            "series_id": "S",
            "date": date(2020, 1, 1),
            "value": value,
            "vintage_date": None,
            "frequency": "M",
            "units": "u",
            "seasonal_adjustment": "SA",
        }

    t1 = pa.Table.from_pylist([row(1.0)], schema=SERIES.schema)
    t2 = pa.Table.from_pylist([row(2.0)], schema=SERIES.schema)
    assert upsert(t1, SERIES, tmp_path, source="x", n_buckets=4).inserted == 1
    assert upsert(t1, SERIES, tmp_path, source="x", n_buckets=4).unchanged == 1
    assert upsert(t2, SERIES, tmp_path, source="x", n_buckets=4).replaced == 1
    out = read_table(SERIES, tmp_path, source="x").to_pylist()
    assert [r["value"] for r in out] == [2.0]


def test_store_recovers_interrupted_swap(tmp_path):
    src = tmp_path / "x"
    (src / "03.old-abc").mkdir(parents=True)
    (src / "03.old-abc" / "part-1.parquet").write_bytes(b"")
    (src / "05.new-def").mkdir()
    recover(src)
    assert (src / "03" / "part-1.parquet").exists()
    assert not (src / "05.new-def").exists()


def test_is_stale(make):
    ing = make()
    assert ing.is_stale("crawl") and ing.is_stale("crawl-meta")
    ing.crawl()
    assert not ing.is_stale("crawl") and ing.is_stale("crawl-meta")
    make.now.t = T0 + timedelta(days=31)
    assert make().is_stale("crawl")


# --- vintages: motivo en el linaje y ventanas que FRED rechaza ---------------------------------


def vintage_lineage(paths):
    from src.schemas import VINTAGE_LINEAGE

    return {
        r["series_id"]: r
        for r in read_table(VINTAGE_LINEAGE, paths.vintage_lineage, source=SOURCE).to_pylist()
    }


def test_vintages_not_in_alfred_has_reason_and_is_not_retried(make, fake):
    fake.not_in_alfred.add("BBB")
    make.cfg["vintages"]["sample_size"] = 0
    ing = prepare(make)
    stats = ing.vintages(ids=["AAA", "BBB"], run_id="v1")
    assert stats["fetched"] == 1 and stats["no_data"] == 1 and stats["series_errors"] == 0
    assert stats["pending"] == 0
    lin = vintage_lineage(make.paths)
    assert lin["AAA"]["status"] == "ok" and lin["AAA"]["n_rows"] > 0
    assert lin["AAA"]["detail"] is None
    assert lin["BBB"]["status"] == "no_existe_en_alfred" and lin["BBB"]["n_rows"] == 0
    assert "does not exist in ALFRED" in lin["BBB"]["detail"]
    # La segunda corrida no vuelve a pedirla mientras no cambie last_updated.
    before = fake.count("series/vintagedates", series_id="BBB")
    assert make().vintages(ids=["AAA", "BBB"], run_id="v2")["to_fetch"] == 0
    assert fake.count("series/vintagedates", series_id="BBB") == before
    # El motivo vigente aparece en los conteos de verificación.
    from src.catalog import summary

    vs = summary(connect(make.paths.processed))["vintage_lineage"]
    assert vs == {"no_existe_en_alfred": {"series": 1, "ids": ["BBB"]}, "ok": {"series": 1}}


def test_vintages_transient_error_is_recorded_and_retried(make, fake):
    fake.missing.add("AAA")  # series/observations responde 400 genérico
    make.cfg["vintages"]["sample_size"] = 0
    ing = prepare(make)
    stats = ing.vintages(ids=["AAA"], run_id="v1")
    assert stats["series_errors"] == 1 and stats["no_data"] == 0
    assert vintage_lineage(make.paths)["AAA"]["status"] == "error_api"
    fake.missing.clear()
    assert make().vintages(ids=["AAA"], run_id="v2")["fetched"] == 1


def test_vintages_window_rejected_by_cap_is_split(make, fake):
    # Como DBAA y FIN14AAVOL: FRED cuenta una fecha más que series/vintagedates.
    fake.max_vintages = 100
    fake.vintage_count_extra = 1
    make.cfg["vintages"]["max_vintage_dates_per_request"] = 100
    make.cfg["vintages"]["sample_size"] = 0
    ing = prepare(make)
    stats = ing.vintages(ids=["AAA"], run_id="v1")
    assert stats["fetched"] == 1 and stats["series_errors"] == 0
    aaa = [r for r in series_rows(make.paths).to_pylist() if r["series_id"] == "AAA"]
    assert len(aaa) == 320  # el mismo resultado que sin el rechazo
    assert vintage_lineage(make.paths)["AAA"]["status"] == "ok"


# --- compactación: una segunda corrida no aumenta el número de archivos ------------------------


def test_second_run_does_not_increase_files(make, fake):
    from src.ingest.store import compact, count_files

    ing = prepare(make)
    ing.download()
    ing.vintages(ids=["AAA", "FFF"], run_id="v1")
    assert count_files(make.paths.processed) > len(list(make.paths.processed.rglob("??")))
    compact(make.paths.processed)
    after_first = count_files(make.paths.processed)
    buckets = [p for p in make.paths.processed.rglob("??") if p.is_dir()]
    assert all(len(list(b.glob("*.parquet"))) <= 1 for b in buckets)
    rows_first = series_rows(make.paths).num_rows

    # Segunda corrida completa con una revisión: más filas, mismos archivos.
    fake.revise("AAA", "2026-08-01", "999.0", on="2026-10-25")
    fake.infos["AAA"]["last_updated"] = "2026-10-25 07:50:02-05"
    fake.updates = ["AAA"]
    fake.today = "2026-10-27"
    make.now.t = T0 + timedelta(days=7)
    ing2 = make()
    ing2.update()
    ing2.vintages(ids=["AAA", "FFF"], run_id="v2")
    run_funnel(make, run_id="run-2")
    res = compact(make.paths.processed)
    assert res.buckets_compacted > 0
    assert count_files(make.paths.processed) == after_first
    assert series_rows(make.paths).num_rows == rows_first + 1  # la nueva vintage de AAA
    # Compactar de nuevo no toca nada.
    again = compact(make.paths.processed)
    assert again.buckets_compacted == 0 and again.files_after == after_first
