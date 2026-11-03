"""Corrida semanal de las fuentes de la Fase 4 (infra/sources_update.sh, ADR 0007)."""

from __future__ import annotations

import os
import subprocess

import yaml

from src.config import REPO_ROOT

INFRA = REPO_ROOT / "infra"


def test_run_actions_runs_sources_after_fred():
    text = (INFRA / "run_actions.sh").read_text()
    assert text.rstrip().endswith("infra/sources_update.sh")


def test_sources_script_syncs_only_its_paths_and_tolerates_pending():
    text = (INFRA / "sources_update.sh").read_text()
    assert "set -- bls bea census" in text
    assert "rc -ne 10" in text  # pendiente por presupuesto no es error
    assert '"processed/series/$src"' in text and '"processed/lineage/eligibility/$src"' in text
    assert "sync_r2.sh \\\n" in text and "infra/sync_r2.sh\n" not in text  # nunca todo data/


def test_workflow_passes_source_secrets_without_failing_fred():
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows/ingesta.yml").read_text())
    env = wf["jobs"]["ingesta"]["env"]
    for name in ("SEC_USER_AGENT", "BEA_API_KEY", "CENSUS_API_KEY"):
        assert env[name] == "${{ secrets." + name + " }}"
    check = next(s for s in wf["jobs"]["ingesta"]["steps"] if s.get("name") == "Verificar secrets")
    assert "::warning::falta el secret $v" in check["run"]


def test_one_failing_source_does_not_stop_the_others(tmp_path):
    """Con `uv` y `sync_r2.sh` falsos: bea falla, bls y census corren y se sincronizan."""
    app = tmp_path / "app"
    (app / "infra").mkdir(parents=True)
    (app / "infra" / "sources_update.sh").write_text((INFRA / "sources_update.sh").read_text())
    (app / "infra" / "sync_r2.sh").write_text('#!/usr/bin/env bash\necho "sync $1" >> "$LOG"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(
        '#!/usr/bin/env bash\nmod="${5}"\necho "run $mod" >> "$LOG"\n'
        'case "$mod" in *bea) exit 3;; *bls) exit 10;; esac\n'
    )
    for p in (app / "infra" / "sources_update.sh", app / "infra" / "sync_r2.sh", bin_dir / "uv"):
        p.chmod(0o755)
    log = tmp_path / "log.txt"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(log)}
    res = subprocess.run(
        ["bash", str(app / "infra" / "sources_update.sh")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1 and "fallaron bea" in res.stderr
    lines = log.read_text().splitlines()
    assert lines == [
        "run src.ingest.bls",
        "sync processed/series/bls",
        "run src.ingest.bea",
        "sync processed/series/bea",
        "run src.ingest.census",
        "sync processed/series/census",
    ]
