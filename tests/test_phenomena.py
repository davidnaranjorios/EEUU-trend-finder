"""Agrupación en dos niveles (auditoría f9189307): clusters → fenómenos, triangulados juntos."""

from __future__ import annotations

from datetime import date

import pytest

from src.rank.phenomena import PhenomenaParams, code_prefixes, group_phenomena
from src.rank.triangulate import Candidate, Evidence, assess
from src.schemas import RANK_PHENOMENA, SchemaError, validate

PARAMS = PhenomenaParams(3, 0.25, 0.6)


def cluster(cid, source, sid, agency, effect=2.0, ws=date(2020, 1, 1), we=date(2024, 1, 1)):
    ev = Evidence(
        "series", source, sid, frozenset({agency}), 1e-6, effect, ("slope",), True, ws, we
    )
    return assess(Candidate(cid, "series", [ev], 1), 2), (source, sid)


def build(specs, naics):
    cands, members, links = [], {}, {}
    for cid, source, sid, agency, effect, ws, we in specs:
        c, key = cluster(cid, source, sid, agency, effect, ws, we)
        cands.append(c)
        members[cid] = [key]
        if key in naics:
            links[key] = [(naics[key], 1.0, "codigo_en_metadatos")]
    return cands, members, links


W1 = (date(2020, 1, 1), date(2024, 1, 1))
W2 = (date(2022, 1, 1), date(2025, 1, 1))
FAR = (date(2010, 1, 1), date(2012, 1, 1))


def test_two_clusters_of_different_origin_same_naics_and_direction_triangulate():
    cands, members, links = build(
        [
            ("c1", "bls", "A", "bls", 2.0, *W1),
            ("c2", "census", "B", "census", 1.5, *W2),
        ],
        {("bls", "A"): "5112", ("census", "B"): "5415"},  # 3 dígitos: 511 vs 541 NO comparten
    )
    out = group_phenomena(cands, members, links, PARAMS, 2)
    assert len(out) == 2 and all(p.candidate.status == "una_fuente" for p in out)

    links = {("bls", "A"): [("5415", 1.0, "x")], ("census", "B"): [("54151", 1.0, "x")]}
    out = group_phenomena(cands, members, links, PARAMS, 2)
    assert len(out) == 1
    (p,) = out
    assert p.cluster_ids == ["c1", "c2"] and p.direction == "sube"
    assert p.candidate.status == "candidato" and p.candidate.n_sources == 2
    assert all(c.status == "una_fuente" for c in cands)  # solo triangula al juntarlos


def test_opposite_direction_or_disjoint_windows_do_not_merge():
    naics = {("bls", "A"): "5415", ("census", "B"): "5415"}
    cands, members, links = build(
        [("c1", "bls", "A", "bls", 2.0, *W1), ("c2", "census", "B", "census", -2.0, *W2)], naics
    )
    out = group_phenomena(cands, members, links, PARAMS, 2)
    assert len(out) == 2 and not any(p.candidate.status == "candidato" for p in out)
    cands, members, links = build(
        [("c1", "bls", "A", "bls", 2.0, *W1), ("c2", "census", "B", "census", 2.0, *FAR)], naics
    )
    assert len(group_phenomena(cands, members, links, PARAMS, 2)) == 2


def test_same_origin_clusters_merge_but_stay_one_source():
    # Regla de origen intacta: dos clusters con el mismo origen no suman fuentes.
    naics = {("bls", "A"): "5415", ("fred", "B"): "5415"}
    cands, members, links = build(
        [("c1", "bls", "A", "bls", 2.0, *W1), ("c2", "fred", "B", "bls", 2.0, *W2)], naics
    )
    (p,) = group_phenomena(cands, members, links, PARAMS, 2)
    assert len(p.cluster_ids) == 2 and p.candidate.n_sources == 1
    assert p.candidate.status == "una_fuente"


def test_unanchored_and_mixed_direction_clusters_stay_alone():
    cands, members, links = build(
        [("c1", "bls", "A", "bls", 2.0, *W1), ("c2", "census", "B", "census", 2.0, *W2)],
        {("bls", "A"): "5415"},  # c2 sin NAICS
    )
    out = group_phenomena(cands, members, links, PARAMS, 2)
    assert len(out) == 2


def test_code_prefixes_expand_ranges_and_nesting_matches():
    assert code_prefixes("31-33", 3) == ["31", "32", "33"]
    assert code_prefixes("334513", 3) == ["334"]
    cands, members, links = build(
        [("c1", "bls", "A", "bls", 2.0, *W1), ("c2", "census", "B", "census", 2.0, *W2)],
        {("bls", "A"): "31-33", ("census", "B"): "3345"},
    )
    assert len(group_phenomena(cands, members, links, PARAMS, 2)) == 1  # sector contiene subsector


def test_schema_rejects_phenomenon_with_one_source_marked_candidate():
    import pyarrow as pa

    row = {
        "run_id": "r",
        "phenomenon_id": "p1",
        "status": "candidato",
        "rank": 1,
        "score": 0.5,
        "direction": "sube",
        "naics_json": "[]",
        "window_start": None,
        "window_end": None,
        "cluster_ids_json": "[]",
        "n_clusters": 1,
        "n_members": 1,
        "n_sources": 1,
        "n_sources_public": 1,
        "sources_json": "[]",
        "components_json": None,
        "by_grouping": False,
        "published": False,
    }
    with pytest.raises(SchemaError):
        validate(pa.Table.from_pylist([row], schema=RANK_PHENOMENA.schema), RANK_PHENOMENA)
    validate(
        pa.Table.from_pylist([{**row, "n_sources": 2}], schema=RANK_PHENOMENA.schema),
        RANK_PHENOMENA,
    )


def test_rank_run_writes_a_partition_of_clusters_into_phenomena(tmp_path):
    import json

    from src.catalog import connect
    from src.rank.run import rank_run
    from tests.fakes_rank import build as build_fx

    fx = build_fx(tmp_path)
    summary = rank_run(fx.cfg, "rank_ph")
    con = connect(fx.cfg["paths"]["processed_dir"])
    rows = con.execute(
        "SELECT cluster_ids_json FROM rank_phenomena WHERE run_id = 'rank_ph'"
    ).fetchall()
    in_phen = [c for (j,) in rows for c in json.loads(j)]
    series_clusters = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT cluster_id FROM clusters WHERE run_id = 'rank_ph' "
            "AND cluster_id LIKE 'c%'"
        ).fetchall()
    ]
    assert sorted(in_phen) == sorted(series_clusters)  # cada cluster en exactamente un fenómeno
    assert summary["n_phenomena"] == len(rows) and "n_phenomena_by_grouping" in summary
