from datetime import UTC, date, datetime

import pyarrow as pa
import pytest

from src.schemas import SERIES, SERIES_META, SIGNALS, TERM_FREQ, TEXT_DOCS, SchemaError, validate


def series_rows(**overrides):
    row = {
        "source": "fred",
        "series_id": "S1",
        "date": date(2020, 1, 1),
        "value": 1.0,
        "vintage_date": date(2020, 2, 1),
        "frequency": "M",
        "units": "idx",
        "seasonal_adjustment": "SA",
    }
    row.update(overrides)
    return row


def table(spec, rows):
    return pa.Table.from_pylist(rows, schema=spec.schema)


def test_valid_series_passes():
    rows = [series_rows(), series_rows(date=date(2020, 2, 1))]
    validate(table(SERIES, rows), SERIES)


def test_same_date_different_vintage_is_not_duplicate():
    rows = [series_rows(), series_rows(vintage_date=date(2020, 3, 1))]
    validate(table(SERIES, rows), SERIES)


def test_duplicate_key_rejected():
    with pytest.raises(SchemaError, match="duplicadas"):
        validate(table(SERIES, [series_rows(), series_rows(value=2.0)]), SERIES)


def test_duplicate_with_null_vintage_rejected():
    rows = [series_rows(vintage_date=None), series_rows(vintage_date=None, value=2.0)]
    with pytest.raises(SchemaError, match="duplicadas"):
        validate(table(SERIES, rows), SERIES)


def test_bad_frequency_rejected():
    with pytest.raises(SchemaError, match="frequency"):
        validate(table(SERIES, [series_rows(frequency="X")]), SERIES)


def test_missing_column_rejected():
    t = table(SERIES, [series_rows()]).drop_columns(["units"])
    with pytest.raises(SchemaError, match="faltantes"):
        validate(t, SERIES)


def test_wrong_type_rejected():
    t = table(SERIES, [series_rows()])
    t = t.set_column(t.schema.get_field_index("value"), "value", pa.array(["1.0"]))
    with pytest.raises(SchemaError, match="tipo"):
        validate(t, SERIES)


def test_null_in_required_column_rejected():
    t = pa.Table.from_pylist([series_rows(series_id=None)], schema=SERIES.schema.remove_metadata())
    with pytest.raises(SchemaError, match="nulos"):
        validate(t, SERIES)


def test_series_meta_valid():
    row = {
        "source": "fred",
        "series_id": "S1",
        "title": "t",
        "category_path": "a/b",
        "start_date": date(2000, 1, 1),
        "end_date": date(2020, 1, 1),
        "last_updated": datetime(2020, 1, 2, tzinfo=UTC),
        "discontinued": False,
        "copyrighted": False,
    }
    validate(table(SERIES_META, [row]), SERIES_META)


def test_text_docs_valid():
    row = {
        "source": "edgar",
        "doc_id": "d1",
        "date": date(2020, 1, 1),
        "entity": "e",
        "section": "1A",
        "text_hash": "abc",
    }
    validate(table(TEXT_DOCS, [row]), TEXT_DOCS)


def test_term_freq_inconsistent_counts_rejected():
    row = {"source": "edgar", "period": "2020", "term": "x", "doc_count": 5, "total_docs": 3}
    with pytest.raises(SchemaError, match="inconsistentes"):
        validate(table(TERM_FREQ, [row]), TERM_FREQ)


def signal_row(**overrides):
    row = {
        "signal_id": "sig1",
        "source": "fred",
        "object_id": "S1",
        "detector": "theil_sen",
        "window_start": date(2019, 1, 1),
        "window_end": date(2020, 1, 1),
        "effect_size": 0.3,
        "p_value": 0.01,
        "q_value": 0.04,
        "run_date": date(2020, 2, 1),
        "run_id": "r1",
        "n_tests": 120,
    }
    row.update(overrides)
    return row


def test_signals_valid():
    validate(table(SIGNALS, [signal_row()]), SIGNALS)


@pytest.mark.parametrize(
    "overrides",
    [
        {"p_value": 1.5},
        {"q_value": -0.1},
        {"window_start": date(2021, 1, 1)},
        {"n_tests": 0},
    ],
)
def test_signals_out_of_range_rejected(overrides):
    with pytest.raises(SchemaError, match="signals"):
        validate(table(SIGNALS, [signal_row(**overrides)]), SIGNALS)
