"""Regresión del reporte con particiones previas al co-movimiento."""

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.ingest.store import read_table, upsert
from src.schemas import SERIES_TERM_LINKS


def link(run_id, **kw):
    return {
        "run_id": run_id,
        "source": "fred",
        "series_id": "synthetic",
        "text_source": "edgar",
        "term": "synthetic term",
        "score": 0.8,
        "method": "embedding",
        "comove_q": None,
        "comove_lag": None,
        "validated": None,
        **kw,
    }


@pytest.mark.parametrize("replace", [False, True])
def test_upsert_legacy_links_preserves_unknown_validation(tmp_path, replace):
    spec = SERIES_TERM_LINKS
    bucket = tmp_path / "rank" / "00"
    bucket.mkdir(parents=True)
    old = pa.Table.from_pylist([link("old"), link("kept")], schema=spec.schema)
    pq.write_table(old.drop(["comove_q", "comove_lag", "validated"]), bucket / "old.parquet")
    new = link("old" if replace else "new", comove_q=0.01, comove_lag=1, validated=True)
    table = pa.Table.from_pylist([new], schema=spec.schema)
    result = upsert(table, spec, tmp_path, source="rank", n_buckets=1, bucket_col="run_id")
    assert (result.inserted, result.replaced) == ((0, 1) if replace else (1, 0))
    rows = {r["run_id"]: r for r in read_table(spec, tmp_path, source="rank").to_pylist()}
    assert len(rows) == (2 if replace else 3)
    assert rows["kept"] == link("kept")
    assert rows[new["run_id"]] == new
    again = upsert(table, spec, tmp_path, source="rank", n_buckets=1, bucket_col="run_id")
    assert (again.inserted, again.replaced, again.unchanged) == (0, 0, 1)


def test_upsert_does_not_invent_required_columns(tmp_path):
    spec = SERIES_TERM_LINKS
    bucket = tmp_path / "rank" / "00"
    bucket.mkdir(parents=True)
    old = pa.Table.from_pylist([link("old")], schema=spec.schema).drop(["method"])
    pq.write_table(old, bucket / "old.parquet")
    with pytest.raises(ValueError, match="columna obligatoria method"):
        upsert(
            pa.Table.from_pylist([link("new")], schema=spec.schema),
            spec,
            tmp_path,
            source="rank",
            n_buckets=1,
            bucket_col="run_id",
        )
    assert pq.read_table(bucket / "old.parquet").equals(old)
