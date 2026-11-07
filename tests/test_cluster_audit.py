"""Auditoría local: estados persistidos y muestra humana, sin datos remotos."""

import csv
import json
import runpy
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

# No se importa el paquete eval: el guard prueba que importar src no lo carga.
AUDIT = runpy.run_path(str(Path(__file__).parents[1] / "eval" / "cluster_audit.py"))
inventory, sample, main = (AUDIT[k] for k in ("inventory", "sample", "main"))


def candidate(cid, status="una_fuente", published=False, **kw):
    return {
        "run_id": "r",
        "candidate_id": cid,
        "kind": "series",
        "status": status,
        "published": published,
        "n_sources": 1,
        "n_sources_public": 1,
        "sources_json": '[["origin"]]',
        "rank": None,
        **kw,
    }


def dataset():
    return {
        "clusters": [
            {
                "run_id": "r",
                "cluster_id": "single",
                "source": "fred",
                "object_id": "synthetic",
                "role": "representante",
            }
        ],
        "rank_candidates": [
            candidate("single"),
            candidate("cap", "fuera_del_tope", rank=51, n_sources=2),
            candidate("private", "candidato", n_sources=2),
            candidate("published", "candidato", True, n_sources=2, n_sources_public=2),
        ],
        "rank_evidence": [
            {
                "candidate_id": "single",
                "source": "fred",
                "object_id": "synthetic",
                "agencies_json": '["origin"]',
                "public": False,
            }
        ],
        "rank_origin": [
            {
                "source": "fred",
                "object_id": "synthetic",
                "origin_json": '["origin"]',
                "chain_json": '["publisher", "origin"]',
            }
        ],
        "naics_links": [
            {
                "source": "fred",
                "object_id": "synthetic",
                "naics": "00",
                "metodo": "synthetic",
                "peso": 1.0,
            }
        ],
        "series_term_links": [{"source": "fred", "series_id": "synthetic", "term": "context"}],
        "rank_runs": [
            {"params_json": json.dumps({"rank": {"min_sources": 2, "max_candidates": 50}})}
        ],
    }


def test_inventory_uses_recorded_rules_and_real_origin_without_redoing_rank():
    rows = {r["cluster_id"]: r for r in inventory(dataset(), "r")}
    assert rows["single"]["exclusion_rules"][0] == {
        "rule": "triangulacion",
        "condition": "n_sources < min_sources",
        "observed": 1,
        "threshold": 2,
        "source": "src/rank/triangulate.py:assess",
        "certainty": "estado_persistido",
    }
    assert rows["cap"]["exclusion_rules"][0]["rule"] == "tope"
    assert rows["private"]["exclusion_rules"][0]["rule"] == "fuentes_publicas"
    assert rows["published"]["exclusion_rules"] == []
    member = rows["single"]["members"][0]
    assert member["origin"] == ["origin"] and member["public"] == [False]
    assert member["in_cluster"] is True
    assert rows["single"]["publishers"] == ["fred"]
    assert member["naics"][0]["metodo"] == "synthetic"
    assert rows["single"]["links"][0]["validation_state"] == "no_registrado"
    assert "No se persistieron todos los pares" in rows["single"]["limitations"][0]


def test_no_candidate_or_run_params_are_unknown_not_invented():
    data = dataset()
    data["rank_candidates"] = []
    data["rank_runs"] = []
    row = inventory(data, "r")[0]
    assert row["status"] is None and row["n_sources"] is None
    assert row["exclusion_rules"] == [
        {"rule": "sin_candidato_persistido", "certainty": "desconocido"}
    ]
    data["rank_candidates"] = [candidate("single")]
    assert inventory(data, "r")[0]["exclusion_rules"][0]["threshold"] is None


def test_sample_is_reproducible_capped_stratified_and_includes_published():
    data = {"rank_candidates": [candidate(str(i)) for i in range(30)]}
    data["rank_candidates"].append(candidate("published", "candidato", True))
    rows = inventory(data, "r")
    chosen = sample(rows, seed="test")
    assert len(chosen) == 20 and len({r["cluster_id"] for r in chosen}) == 20
    assert "published" in {r["cluster_id"] for r in chosen}
    assert chosen == sample(list(reversed(rows)), seed="test")
    assert len(sample(rows[:3])) == 3
    assert sample([]) == []
    with pytest.raises(ValueError):
        sample(rows, size=21)


def write(root, table, rows):
    folder = root / table / "rank" / "00"
    folder.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), folder / "part.parquet")


def test_cli_local_legacy_tables_and_labels_do_not_touch_inputs(tmp_path, capsys):
    root, out = tmp_path / "rank", tmp_path / "audit"
    write(
        root,
        "clusters",
        [
            {
                "run_id": "r",
                "cluster_id": "c",
                "source": "fred",
                "object_id": "s",
                "role": "representante",
            }
        ],
    )
    write(root, "rank_candidates", [candidate("c"), {**candidate("other"), "run_id": "other"}])
    write(
        root,
        "series_term_links",
        [{"run_id": "r", "source": "fred", "series_id": "s", "term": "text"}],
    )
    before = {p: p.read_bytes() for p in root.rglob("*.parquet")}
    argv = ["--rank-dir", str(root), "--run-id", "r", "--out", str(out)]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["clusters"] == 1
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["audience"] == "internal_not_for_publication"
    assert "naics_links" in manifest["missing_tables"]
    assert len(manifest["files"]) == 3
    row = json.loads((out / "inventario.json").read_text(encoding="utf-8"))[0]
    assert row["members"][0]["origin"] is None
    assert row["members"][0]["naics"] == []
    with (out / "etiquetas.csv").open(encoding="utf-8", newline="") as f:
        labels = list(csv.DictReader(f))
    assert labels[0]["coherencia"] == "" and labels[0]["cluster_id"] == "c"
    assert {p: p.read_bytes() for p in root.rglob("*.parquet")} == before
    with pytest.raises(FileExistsError):
        main(argv)


def test_empty_or_unsafe_input_never_yields_fake_inventory(tmp_path):
    root, out = tmp_path / "rank", tmp_path / "audit"
    with pytest.raises(SystemExit):
        main(["--rank-dir", str(root), "--run-id", "absent", "--out", str(out)])
    assert not out.exists()
    with pytest.raises(SystemExit):
        main(["--rank-dir", str(root), "--run-id", "r", "--out", str(root / "audit")])


def test_duplicate_candidate_is_not_silently_overwritten():
    with pytest.raises(ValueError, match="duplicado"):
        inventory({"rank_candidates": [candidate("c"), candidate("c")]}, "r")


def test_optional_local_metadata_enriches_only_matching_members(tmp_path):
    root, out, meta = tmp_path / "rank", tmp_path / "audit", tmp_path / "meta"
    write(
        root,
        "clusters",
        [
            {
                "run_id": "r",
                "cluster_id": "c",
                "source": "fred",
                "object_id": "s",
                "role": "representante",
            }
        ],
    )
    part = meta / "fred" / "00"
    part.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "source": "fred",
                    "series_id": "s",
                    "title": "Private synthetic",
                    "copyrighted": True,
                },
                {"source": "fred", "series_id": "other", "title": "Other", "copyrighted": False},
            ]
        ),
        part / "meta.parquet",
    )
    assert (
        main(["--rank-dir", str(root), "--run-id", "r", "--out", str(out), "--meta-dir", str(meta)])
        == 0
    )
    rows = json.loads((out / "inventario.json").read_text(encoding="utf-8"))
    member = rows[0]["members"][0]
    assert member["title"] == "Private synthetic" and member["metadata_copyrighted"] is True
    assert member["units"] is None and member["metadata_status"] == "available"
    assert len(rows[0]["members"]) == 1
    assert "Other" not in (out / "inventario.json").read_text(encoding="utf-8")


def test_naics_summary_counts_unique_objects_per_method_for_whole_run():
    summarize = AUDIT["naics_summary"]
    data = {
        # No cluster/evidence membership for these links: the summary must still count them.
        "naics_links": [
            {"source": "bls", "object_id": "a", "naics": "00", "metodo": "explicit", "peso": 0.5},
            {"source": "bls", "object_id": "a", "naics": "01", "metodo": "explicit", "peso": 0.5},
            {"source": "bls", "object_id": "a", "naics": "00", "metodo": "inherited", "peso": 1.0},
            {"source": "bls", "object_id": "b", "naics": "00", "metodo": "explicit", "peso": 1.0},
        ],
        "rank_runs": [
            {
                "params_json": json.dumps(
                    {
                        "summary": {
                            "naics_coverage": {
                                "bls": {"n": 10, "with_naics": 2, "pct": 20.0},
                                "edgar": {"n": 306, "with_naics": 0, "pct": 0.0},
                            }
                        }
                    }
                )
            }
        ],
    }
    result = summarize(data)
    bls, edgar = (result["sources"][s] for s in ("bls", "edgar"))
    assert bls["mapped_objects_in_links"] == 2
    assert bls["unique_objects_by_method"] == {"explicit": 2, "inherited": 1}
    assert bls["signal_denominator"] == 10
    assert bls["stored_signal_coverage"] == {"n": 10, "with_naics": 2, "pct": 20.0}
    assert edgar["mapped_objects_in_links"] == 0 and edgar["signal_denominator"] == 306
    assert "no sumar" in result["limitations"][0]


def test_naics_summary_does_not_invent_denominator_from_clusters_or_missing_links():
    summarize = AUDIT["naics_summary"]
    data = {"naics_links": [{"source": "fred", "object_id": "s", "metodo": "inherited"}]}
    result = summarize(data)
    assert not result["stored_coverage_available"]
    assert result["sources"]["fred"]["signal_denominator"] is None
    assert result["sources"]["fred"]["stored_signal_coverage"] is None
    data = {
        "rank_runs": [
            {
                "params_json": json.dumps(
                    {
                        "summary": {
                            "naics_coverage": {"fred": {"n": 146, "with_naics": 25, "pct": 17.1}}
                        }
                    }
                )
            }
        ]
    }
    result = summarize(data, links_available=False)
    assert not result["links_available"]
    assert result["sources"]["fred"]["mapped_objects_in_links"] is None
    assert result["sources"]["fred"]["unique_objects_by_method"] is None
    assert result["sources"]["fred"]["signal_denominator"] == 146
