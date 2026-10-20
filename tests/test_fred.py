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

from src.catalog import connect
from src.config import load_config
from src.ingest.fred import FETCH_STATE, FRED_CATALOG, SOURCE, FredIngestor, Paths, eligibility
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
    return f


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["fred"].update(page_limit_series=2, page_limit_observations=50, obs_batch_size=2, n_buckets=4)
    return c


@pytest.fixture
def make(fake, cfg, tmp_path):
    paths = Paths(tmp_path / "processed", tmp_path / "raw")
    clock = FakeClock()
    now = Now(T0)

    def _make() -> FredIngestor:
        client = ApiClient(
            "https://api.stlouisfed.org/fred/",
            {"api_key": API_KEY},
            requests_per_minute=6000,
            max_retries=3,
            timeout_s=5,
            backoff_base_s=1.0,
            raw_dir=paths.raw / SOURCE,
            transport=fake.transport(),
            clock=clock,
            sleep=clock.sleep,
            today=lambda: now().date(),
            rng=random.Random(0),
        )
        return FredIngestor(client, cfg, paths, now=now)

    _make.paths = paths
    _make.now = now
    _make.clock = clock
    return _make


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


def test_eligibility_counts(make, cfg):
    make().crawl()
    catalog = read_table(FRED_CATALOG, make.paths.catalog, source=SOURCE).to_pylist()
    eligible, counts = eligibility(catalog, cfg, T0.date())
    assert sorted(r["series_id"] for r in eligible) == ["AAA", "BBB", "FFF"]
    assert counts["discontinued"] == 1
    assert counts["unsupported_frequency"] == {"SA": 1}
    assert counts["too_short"] == 1


def test_eligibility_uses_config(make, cfg):
    make().crawl()
    catalog = read_table(FRED_CATALOG, make.paths.catalog, source=SOURCE).to_pylist()
    cfg["eligibility"]["min_obs"]["M"] = 10_000
    eligible, counts = eligibility(catalog, cfg, T0.date())
    assert eligible == [] and counts["too_short"] == 4
    cfg["eligibility"]["min_obs"]["M"] = 120
    _, counts = eligibility(catalog, cfg, T0.date() + timedelta(days=400))
    assert counts["stale"] == 3 and counts["too_short"] == 1  # EEE se descarta antes


def test_pagination_observations(make, fake):
    ing = make()
    ing.crawl()
    stats = ing.observations()
    assert stats["fetched"] == 3
    # 320 observaciones con page_limit_observations=50: 7 páginas por serie.
    assert fake.count("series/observations", series_id="AAA") == 7
    rows = series_rows(make.paths)
    assert rows.num_rows == 3 * 320
    validate(rows, SERIES)


def test_vintage_date_is_publication_date(make):
    ing = make()
    ing.crawl()
    ing.observations()
    row = next(
        r
        for r in series_rows(make.paths).to_pylist()
        if r["series_id"] == "AAA" and r["date"] == date(2000, 1, 1)
    )
    assert row["vintage_date"] == date(2000, 2, 15)  # realtime_start, no la fecha de hoy
    assert row["frequency"] == "M" and row["units"] == "Index"


def test_fred_idempotent(make):
    ing = make()
    ing.crawl()
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


def test_unsupported_frequency_not_fetched(make, fake):
    ing = make()
    ing.crawl()
    stats = ing.observations()
    assert stats["eligibility"]["unsupported_frequency"] == {"SA": 1}
    assert fake.count("series/observations", series_id="DDD") == 0


def test_api_errors_per_series_are_counted(make, fake):
    fake.missing.add("BBB")
    ing = make()
    ing.crawl()
    stats = ing.observations()
    assert stats["series_errors"] == 1 and stats["fetched"] == 2
    # Sin estado guardado para BBB: se reintentará en la próxima corrida.
    state = read_table(FETCH_STATE, make.paths.fetch_state, source=SOURCE).to_pylist()
    assert "BBB" not in {r["series_id"] for r in state}


def test_vintage_cap_fallback(make, fake):
    fake.vintage_cap.add("AAA")
    ing = make()
    ing.crawl()
    stats = ing.observations()
    assert stats["vintage_cap_fallbacks"] == 1
    state = {
        r["series_id"]: r
        for r in read_table(FETCH_STATE, make.paths.fetch_state, source=SOURCE).to_pylist()
    }
    floor = state["AAA"]["realtime_floor"]
    assert floor == date(2026, 9, 15)  # última vintage de AAA
    aaa = [r for r in series_rows(make.paths).to_pylist() if r["series_id"] == "AAA"]
    assert len(aaa) == 320 and all(r["vintage_date"] >= floor for r in aaa)


# --- update / incremental ----------------------------------------------------------------------


def test_fred_incremental(make, fake):
    ing = make()
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


def test_revision_appends_vintage(make, fake):
    ing = make()
    ing.update()
    fake.revise("AAA", "2026-08-01", "999.0", on="2026-10-25")
    fake.infos["AAA"]["last_updated"] = "2026-10-25 07:50:02-05"
    fake.updates = ["AAA"]
    make.now.t = T0 + timedelta(days=7)
    make().update()

    con = connect(make.paths.processed)
    rows = con.execute(
        "SELECT vintage_date, value FROM series WHERE series_id='AAA' AND date='2026-08-01' "
        "ORDER BY vintage_date"
    ).fetchall()
    assert rows == [(date(2026, 9, 15), 259.5), (date(2026, 10, 25), 999.0)]
    # Vista "tal como se conocía" en una fecha: sin información del futuro.
    as_of = con.execute(
        "SELECT value FROM series WHERE series_id='AAA' AND date='2026-08-01' "
        "AND vintage_date <= DATE '2026-10-01' ORDER BY vintage_date DESC LIMIT 1"
    ).fetchone()
    assert as_of == (259.5,)


def test_update_falls_back_to_crawl_after_gap(make, fake):
    make().update()
    make.now.t = T0 + timedelta(days=20)  # más que la ventana de series/updates
    stats = make().update()
    assert stats["mode"] == "crawl_fallback"


def test_retries_exhausted_abort_and_cursor_not_advanced(make, fake, tmp_path):
    make().update()
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


def test_api_key_never_written(make, caplog):
    caplog.set_level(logging.DEBUG)
    ing = make()
    ing.crawl()
    ing.observations()
    for path in make.paths.raw.rglob("*.json.gz"):
        import gzip

        assert API_KEY not in gzip.open(path, "rt").read()
    assert API_KEY not in caplog.text


# --- consulta DuckDB y almacén -------------------------------------------------------------


def test_duckdb_category_query(make):
    ing = make()
    ing.crawl()
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
