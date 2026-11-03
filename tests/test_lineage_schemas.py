"""Tablas de linaje (ADR 0002): esquemas válidos y rechazo de datos inválidos."""

from datetime import UTC, datetime

import pyarrow as pa
import pytest

from src.schemas import (
    CLUSTERS,
    DETECTOR_RUNS,
    ELIGIBILITY,
    SERIES_TERM_LINKS,
    VINTAGE_LINEAGE,
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
    VINTAGE_LINEAGE.name: {
        "run_id": "r1",
        "source": "fred",
        "series_id": "S1",
        "status": "no_existe_en_alfred",
        "n_rows": 0,
        "detail": "series/vintagedates: HTTP 400: The series does not exist in ALFRED",
        "decided_at": T,
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
SPECS = {
    s.name: s for s in (ELIGIBILITY, VINTAGE_LINEAGE, DETECTOR_RUNS, SERIES_TERM_LINKS, CLUSTERS)
}

INVALID = [
    (ELIGIBILITY.name, {"group_name": "favoritas"}),
    (ELIGIBILITY.name, {"rule": ""}),
    (ELIGIBILITY.name, {"series_id": None}),
    (VINTAGE_LINEAGE.name, {"status": "otro"}),
    (VINTAGE_LINEAGE.name, {"detail": None}),  # sin datos y sin motivo
    (VINTAGE_LINEAGE.name, {"status": "ok", "detail": "x"}),
    (VINTAGE_LINEAGE.name, {"n_rows": -1}),
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
