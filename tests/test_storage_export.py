"""ADR 0002: lectura desde R2 (SQL generado) y exportes para la web estática."""

import copy
import json
from datetime import UTC, date, datetime

import pyarrow as pa
import pytest

from src import eligibility as el
from src.catalog import connect, r2_statements
from src.config import load_config
from src.export import ExportTooLarge, export_all
from src.ingest.fred import FRED_CATALOG, SERIES_ATTRS, SOURCE, Paths
from src.ingest.store import compact, count_files, read_table, recover, upsert

ENV = {
    "CLOUDFLARE_ACCOUNT_ID": "0123456789abcdef0123456789abcdef",
    "AWS_ACCESS_KEY_ID": "kid",
    "AWS_SECRET_ACCESS_KEY": "s'ecret",
}


def test_r2_statements_views_and_secret():
    stmts = r2_statements({"bucket": "b", "prefix": "/processed/"}, ENV)
    assert stmts[:2] == ["INSTALL httpfs", "LOAD httpfs"]
    assert "TYPE r2" in stmts[2] and "'s''ecret'" in stmts[2]  # comilla escapada
    views = {s.split()[4]: s for s in stmts if s.startswith("CREATE OR REPLACE VIEW")}
    assert set(views) == {"series", "series_meta", "eligibility", "vintage_lineage"}
    assert "'r2://b/processed/series/*/*/*.parquet'" in views["series"]
    assert "'r2://b/processed/lineage/eligibility/*/*/*.parquet'" in views["eligibility"]


def test_r2_requires_credentials():
    with pytest.raises(RuntimeError, match="AWS_SECRET_ACCESS_KEY"):
        r2_statements({"bucket": "b", "prefix": "p"}, {**ENV, "AWS_SECRET_ACCESS_KEY": ""})


def catalog_row(sid, **kw):
    r = {
        "source": SOURCE,
        "series_id": sid,
        "title": f"Serie {sid}",
        "frequency_short": "M",
        "units": "Index",
        "seasonal_adjustment": "SA",
        "observation_start": date(1990, 1, 1),
        "observation_end": date(2026, 9, 1),
        "last_updated": datetime(2026, 10, 1, tzinfo=UTC),
        "category_ids": [1],
        "category_path": "A > B",
        "notes_present": True,
        "discontinued": False,
        "copyrighted": False,
    }
    r.update(kw)
    return r


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path / "processed", tmp_path / "raw")
    rows = [
        catalog_row("N1"),
        catalog_row("N2", copyrighted=True),
        catalog_row("X", discontinued=True),
    ]
    upsert(
        pa.Table.from_pylist(rows, schema=FRED_CATALOG.schema),
        FRED_CATALOG,
        p.catalog,
        source=SOURCE,
        n_buckets=4,
    )
    attrs = [
        {
            "source": SOURCE,
            "series_id": s,
            "geo_types": ["nation"],
            "is_usa": True,
            "release_ids": [1],
            "agencies": ["A"],
            "popularity": 1,
        }
        for s in ("N1", "N2", "X")
    ]
    upsert(
        pa.Table.from_pylist(attrs, schema=SERIES_ATTRS.schema),
        SERIES_ATTRS,
        p.series_attrs,
        source=SOURCE,
        n_buckets=4,
    )
    cfg = copy.deepcopy(load_config())
    catalog, at = el.load_inputs(p)
    decisions = el.classify(catalog, at, cfg, date(2026, 10, 21))
    el.write_lineage(decisions, "r1", datetime(2026, 10, 21, tzinfo=UTC), p, n_buckets=4)
    return p


EXPORTS = {"groups": ["nacional"], "n_buckets": 4, "max_file_kb": 512, "max_total_mb": 20}


def read_cards(out):
    cards = []
    for p in sorted((out / "series").glob("0*.json")):
        body = json.loads(p.read_text())
        cards += [dict(zip(body["columns"], row, strict=True)) for row in body["rows"]]
    return cards


def test_export_funnel_and_cards(paths, tmp_path):
    out = tmp_path / "exports"
    index = export_all(paths, out, EXPORTS, v2_page_limit=500000)
    assert {k: index[k] for k in ("run_id", "n_buckets", "cards", "copyrighted_withheld")} == {
        "run_id": "r1",
        "n_buckets": 4,
        "cards": 1,
        "copyrighted_withheld": 1,
    }
    f = json.loads((out / "funnel.json").read_text())
    assert f["grupos"] == {"nacional": 2, "excluida": 1}
    cards = read_cards(out)
    assert [c["id"] for c in cards] == ["N1"]  # N2 tiene copyright: no se publica
    assert cards[0]["start"] == "1990-01-01" and cards[0]["last_updated"] == "2026-10-01"
    categories = json.loads((out / "categories.json").read_text())["paths"]
    assert [categories[i] for i in cards[0]["categories"]] == ["A > B"]
    on_disk = sum(p.stat().st_size for p in out.rglob("*.json"))
    assert index["total_bytes"] == on_disk


def test_export_only_configured_groups(paths, tmp_path):
    out = tmp_path / "exports"
    index = export_all(paths, out, {**EXPORTS, "groups": ["regional"]}, v2_page_limit=500000)
    assert index["cards"] == 0 and index["copyrighted_withheld"] == 0
    assert read_cards(out) == []


def test_export_respects_size_caps(paths, tmp_path):
    out = tmp_path / "exports"
    index = export_all(paths, out, EXPORTS, v2_page_limit=500000)
    assert index["max_file_bytes"] <= EXPORTS["max_file_kb"] * 1024
    assert index["total_bytes"] <= EXPORTS["max_total_mb"] * 1024 * 1024
    for p in out.rglob("*.json"):
        assert p.stat().st_size <= EXPORTS["max_file_kb"] * 1024
    # Con un tope menor que funnel.json falla sin escribir nada.
    small = tmp_path / "small"
    with pytest.raises(ExportTooLarge, match="max_file_kb"):
        export_all(paths, small, {**EXPORTS, "max_file_kb": 0.1}, v2_page_limit=500000)
    assert not small.exists()
    with pytest.raises(ExportTooLarge, match="max_total_mb"):
        export_all(paths, small, {**EXPORTS, "max_total_mb": 0.001}, v2_page_limit=500000)
    assert not small.exists()


def test_export_removes_stale_buckets(paths, tmp_path):
    out = tmp_path / "exports"
    (out / "series").mkdir(parents=True)
    (out / "series" / "63.json").write_text("{}")
    export_all(paths, out, EXPORTS, v2_page_limit=500000)
    assert not (out / "series" / "63.json").exists()


# --- compactación (ADR 0004) --------------------------------------------------------------------


def test_compact_merges_parts_and_keeps_rows(tmp_path):
    root = tmp_path / "processed"
    spec_root = root / "catalog"
    for i in range(3):  # tres upserts con filas nuevas -> tres archivos por bucket
        rows = [catalog_row(f"S{i}{j}") for j in range(5)]
        upsert(
            pa.Table.from_pylist(rows, schema=FRED_CATALOG.schema),
            FRED_CATALOG,
            spec_root,
            source=SOURCE,
            n_buckets=1,
        )
    before = read_table(FRED_CATALOG, spec_root, source=SOURCE)
    assert count_files(root) == 3
    res = compact(root)
    assert (res.buckets_compacted, res.files_before, res.files_after, res.rows) == (1, 3, 1, 15)
    after = read_table(FRED_CATALOG, spec_root, source=SOURCE)
    assert sorted(after.to_pylist(), key=lambda r: r["series_id"]) == sorted(
        before.to_pylist(), key=lambda r: r["series_id"]
    )
    assert compact(root).buckets_compacted == 0  # idempotente
    # Un upsert posterior sigue funcionando sobre el bucket compactado.
    upsert(
        pa.Table.from_pylist([catalog_row("S00", title="nuevo")], schema=FRED_CATALOG.schema),
        FRED_CATALOG,
        spec_root,
        source=SOURCE,
        n_buckets=1,
    )
    titles = {
        r["series_id"]: r["title"]
        for r in read_table(FRED_CATALOG, spec_root, source=SOURCE).to_pylist()
    }
    assert titles["S00"] == "nuevo" and len(titles) == 15


def test_compact_recovers_interrupted_swap(tmp_path):
    root = tmp_path / "processed"
    spec_root = root / "catalog"
    for i in range(2):
        upsert(
            pa.Table.from_pylist([catalog_row(f"S{i}")], schema=FRED_CATALOG.schema),
            FRED_CATALOG,
            spec_root,
            source=SOURCE,
            n_buckets=1,
        )
    bucket = spec_root / SOURCE / "00"
    bucket.rename(bucket.with_name("00.old-x"))  # corte a mitad de un swap
    compact(root)
    recover(spec_root / SOURCE)
    assert read_table(FRED_CATALOG, spec_root, source=SOURCE).num_rows == 2
    assert count_files(root) == 1


def test_local_views_include_lineage(paths):
    con = connect(paths.processed)
    rows = con.execute(
        "SELECT group_name, rule, count(*) FROM eligibility GROUP BY ALL ORDER BY 1"
    ).fetchall()
    assert rows == [("excluida", "descontinuada", 1), ("nacional", "geografia", 2)]


def test_r2_account_id_from_pasted_endpoint():
    env = {
        **ENV,
        "CLOUDFLARE_ACCOUNT_ID": " https://0123456789ABCDEF0123456789abcdef.r2.cloudflarestorage.com\n",
    }
    stmt = r2_statements({"bucket": "b", "prefix": "p"}, env)[2]
    assert "ACCOUNT_ID '0123456789abcdef0123456789abcdef'" in stmt
    with pytest.raises(RuntimeError, match="quedan 3"):
        r2_statements({"bucket": "b", "prefix": "p"}, {**ENV, "CLOUDFLARE_ACCOUNT_ID": "acc"})


def test_summary_counts(paths):
    from src.catalog import summary

    s = summary(connect(paths.processed))
    assert s["eligibility"] == {
        "fred": {"last_run_id": "r1", "groups": {"nacional": 2, "excluida": 1}, "lineage_rows": 3}
    }
    assert s["series"]["rows"] == 0 and s["series_meta"] == 0
