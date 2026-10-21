"""ADR 0002: lectura desde R2 (SQL generado) y exportes para la web estática."""

import copy
import json
from datetime import UTC, date, datetime

import pyarrow as pa
import pytest

from src import eligibility as el
from src.catalog import connect, r2_statements
from src.config import load_config
from src.export import export_all
from src.ingest.fred import FRED_CATALOG, SERIES_ATTRS, SOURCE, Paths
from src.ingest.store import upsert

ENV = {
    "CLOUDFLARE_ACCOUNT_ID": "acc",
    "AWS_ACCESS_KEY_ID": "kid",
    "AWS_SECRET_ACCESS_KEY": "s'ecret",
}


def test_r2_statements_views_and_secret():
    stmts = r2_statements({"bucket": "b", "prefix": "/processed/"}, ENV)
    assert stmts[:2] == ["INSTALL httpfs", "LOAD httpfs"]
    assert "TYPE r2" in stmts[2] and "'s''ecret'" in stmts[2]  # comilla escapada
    views = {s.split()[4]: s for s in stmts if s.startswith("CREATE OR REPLACE VIEW")}
    assert set(views) == {"series", "series_meta", "eligibility"}
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


def test_export_funnel_and_cards(paths, tmp_path):
    out = tmp_path / "exports"
    index = export_all(paths, out, n_buckets=4, v2_page_limit=500000)
    assert index == {"run_id": "r1", "n_buckets": 4, "cards": 1, "copyrighted_withheld": 1}
    f = json.loads((out / "funnel.json").read_text())
    assert f["grupos"] == {"nacional": 2, "excluida": 1}
    cards = [
        c for p in (out / "series").glob("0*.json") for c in json.loads(p.read_text())["series"]
    ]
    assert [c["id"] for c in cards] == ["N1"]  # N2 tiene copyright: no se publica
    assert cards[0]["start"] == "1990-01-01" and cards[0]["categories"] == ["A > B"]


def test_local_views_include_lineage(paths):
    con = connect(paths.processed)
    rows = con.execute(
        "SELECT group_name, rule, count(*) FROM eligibility GROUP BY ALL ORDER BY 1"
    ).fetchall()
    assert rows == [("excluida", "descontinuada", 1), ("nacional", "geografia", 2)]
