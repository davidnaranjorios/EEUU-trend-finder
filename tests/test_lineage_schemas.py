"""Tablas de linaje (ADR 0002): esquemas válidos y rechazo de datos inválidos."""

from datetime import UTC, datetime

import pyarrow as pa
import pytest

from src.schemas import (
    CLUSTERS,
    DETECTOR_RUNS,
    ELIGIBILITY,
    SERIES_TERM_LINKS,
    SchemaError,
    validate,
)

T = datetime(2026, 10, 21, tzinfo=UTC)

VALID = {
    ELIGIBILITY.name: {
        "run_id": "r1",
        "source": "fred",
        "series_id": "S1",
        "group_name": "nacional",
        "rule": "geografia",
        "detail": None,
        "geo_level": "nacional",
        "frequency": "M",
        "copyrighted": False,
        "decided_at": T,
    },
    DETECTOR_RUNS.name: {
        "run_id": "r1",
        "detector": "theil_sen",
        "started_at": T,
        "finished_at": T,
        "n_tests": 100,
        "n_bh": 3,
        "n_by": 1,
        "q": 0.05,
        "params_json": "{}",
    },
    SERIES_TERM_LINKS.name: {
        "run_id": "r1",
        "source": "fred",
        "series_id": "S1",
        "text_source": "edgar",
        "term": "x",
        "score": 0.8,
        "method": "embeddings",
    },
    CLUSTERS.name: {
        "run_id": "r1",
        "cluster_id": "c1",
        "source": "fred",
        "object_id": "S1",
        "role": "representante",
        "similarity": 1.0,
    },
}
SPECS = {s.name: s for s in (ELIGIBILITY, DETECTOR_RUNS, SERIES_TERM_LINKS, CLUSTERS)}

INVALID = [
    (ELIGIBILITY.name, {"group_name": "favoritas"}),
    (ELIGIBILITY.name, {"rule": ""}),
    (ELIGIBILITY.name, {"series_id": None}),
    (DETECTOR_RUNS.name, {"n_bh": 101}),
    (DETECTOR_RUNS.name, {"q": 1.0}),
    (SERIES_TERM_LINKS.name, {"score": 1.5}),
    (CLUSTERS.name, {"role": "otro"}),
]


def table(name, rows):
    return pa.Table.from_pylist(rows, schema=SPECS[name].schema)


@pytest.mark.parametrize("name", sorted(VALID))
def test_lineage_valid(name):
    validate(table(name, [VALID[name]]), SPECS[name])


@pytest.mark.parametrize(("name", "overrides"), INVALID)
def test_lineage_invalid_rejected(name, overrides):
    with pytest.raises((SchemaError, pa.ArrowInvalid)):
        validate(table(name, [{**VALID[name], **overrides}]), SPECS[name])


@pytest.mark.parametrize("name", sorted(VALID))
def test_lineage_duplicate_key_rejected(name):
    with pytest.raises(SchemaError, match="duplicadas"):
        validate(table(name, [VALID[name], VALID[name]]), SPECS[name])
