"""Contrato del workflow interno; nunca usa credenciales ni almacenamiento real."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "auditoria-clusters.yml"
WF = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
BASH = (
    str(Path("C:/Program Files/Git/bin/bash.exe"))
    if sys.platform == "win32" and Path("C:/Program Files/Git/bin/bash.exe").is_file()
    else shutil.which("bash")
    if sys.platform != "win32"
    else None
)
JOB = WF["jobs"]["auditar"]
STEPS = {s["id"]: s for s in JOB["steps"] if "id" in s}


def test_manual_only_read_permissions_and_no_public_outputs():
    # PyYAML YAML1.1 interpreta `on` como True; no cambiar el YAML GitHub por ese detalle.
    events = WF.get("on", WF.get(True))
    assert set(events) == {"workflow_dispatch"}
    entry = events["workflow_dispatch"]["inputs"]["rank_run_id"]
    assert entry["required"] and entry["type"] == "string"
    assert WF["permissions"] == {"contents": "read"}
    assert WF["concurrency"] == {"group": "reporte", "cancel-in-progress": False}
    assert JOB["timeout-minutes"] == 30
    actions = [s["uses"] for s in JOB["steps"] if "uses" in s]
    assert actions == ["actions/checkout@v6", "astral-sh/setup-uv@v7"]
    assert not any("environment" in j for j in WF["jobs"].values())
    assert JOB["env"]["RANK_RUN_ID"] == "${{ inputs.rank_run_id }}"
    assert all("${{" not in s.get("run", "") for s in JOB["steps"])
    assert JOB["steps"][0]["id"] == "validate"  # validación antes de código/dependencias


def tokens_for(script, command):
    for line in script.replace("\\\n", " ").splitlines():
        if command in line:
            return shlex.split(line[line.index(command) :].split(">", 1)[0])
    raise AssertionError(f"comando ausente: {command}")


def test_only_expected_r2_inputs_and_immutable_fresh_output():
    assert tokens_for(STEPS["pull"]["run"], "infra/pull_r2.sh") == [
        "infra/pull_r2.sh",
        "processed/rank",
        "processed/series_meta",
    ]
    store = STEPS["store"]["run"]
    assert "source infra/r2_env.sh" in store
    assert (
        'destination="r2:$BUCKET/audits/$RANK_RUN_ID/$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT/"' in store
    )
    assert tokens_for(store, "rclone copy") == [
        "rclone",
        "copy",
        "work/audit-output",
        "$destination",
        "--immutable",
        "--checksum",
    ]
    assert tokens_for(store, "rclone check") == [
        "rclone",
        "check",
        "work/audit-output",
        "$destination",
        "--one-way",
        "--checksum",
    ]
    inventory = tokens_for(STEPS["inventory"]["run"], "uv run")
    assert inventory == [
        "uv",
        "run",
        "--quiet",
        "python",
        "eval/cluster_audit.py",
        "--rank-dir",
        "data/processed/rank",
        "--run-id",
        "$RANK_RUN_ID",
        "--meta-dir",
        "data/processed/series_meta",
        "--out",
        "work/audit-output",
    ]
    all_runs = "\n".join(s.get("run", "") for s in JOB["steps"])
    assert "rclone sync" not in all_runs and "sync_r2.sh" not in all_runs
    assert "src.rank" not in all_runs and "src.detect" not in all_runs
    assert "--group embed" not in all_runs


@pytest.mark.skipif(BASH is None, reason="requiere bash")
@pytest.mark.parametrize(
    "value", ["A" * 32, "0" * 31, "0" * 33, "../../x", "$(touch injected)", "a; touch injected"]
)
def test_invalid_input_is_rejected_without_shell_execution(tmp_path, value):
    result = subprocess.run(
        [BASH, "-euo", "pipefail", "-c", STEPS["validate"]["run"]],
        cwd=tmp_path,
        env={**os.environ, "RANK_RUN_ID": value, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "injected").exists()
    assert value not in result.stdout


@pytest.mark.skipif(BASH is None, reason="requiere bash")
def test_valid_input_and_all_run_blocks_have_valid_bash_syntax(tmp_path):
    result = subprocess.run(
        [BASH, "-euo", "pipefail", "-c", STEPS["validate"]["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "RANK_RUN_ID": "a" * 32,
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    for step in JOB["steps"]:
        if "run" in step:
            subprocess.run([BASH, "-n"], input=step["run"], text=True, check=True)


def test_aggregate_summary_never_prints_identifiers_titles_paths_or_unknown_names(tmp_path):
    marker = "SENSITIVE-MARKER"
    folder = tmp_path / "work" / "audit-output"
    folder.mkdir(parents=True)
    coverage_row = {
        "signal_denominator": 168,
        "mapped_objects_in_links": 25,
        "stored_signal_coverage": {"n": 168, "with_naics": 25, "pct": 14.9, "title": marker},
        "unique_objects_by_method": {"codigo_en_metadatos": 25, marker: 900},
        "members": [marker],
    }
    manifest = {
        "n_inventory_records": 119,
        "n_persisted_clusters": 119,
        "sample_size": 20,
        "counts_by_status": {"una_fuente": 118, "candidato": 1, marker: 1},
        "files": [{"path": marker}],
        "title": marker,
        "origins": marker,
        "naics_coverage": {
            "links_available": True,
            "stored_coverage_available": False,
            "sources": {"bls": coverage_row, marker: coverage_row},
        },
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    code = STEPS["summary"]["run"].split("\n", 1)[1].split("\nPY\n", 1)[0]
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert marker not in result.stdout and marker not in result.stderr
    output = json.loads(result.stdout)
    assert output["counts_by_status"] == {"una_fuente": 118, "candidato": 1}
    assert output["n_inventory_records"] == output["n_persisted_clusters"] == 119
    assert output["naics_coverage"]["bls"]["unique_objects_by_method"] == {
        "codigo_en_metadatos": 25
    }
    assert output["naics_coverage"]["bls"]["signal_denominator"] == 168
    assert output["links_available"] is True
    assert output["stored_coverage_available"] is False
    assert output["unknown_sources_present"] and output["unknown_statuses_present"]
    assert output["naics_coverage"]["bls"]["unknown_methods_present"]
