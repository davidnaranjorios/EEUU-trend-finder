"""Cadena semanal de ingesta (ADR 0027): workflows separados, presupuestos y tablas de texto."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import yaml

from src import catalog
from src.config import REPO_ROOT
from src.ingest.text import TextState

WF = REPO_ROOT / ".github" / "workflows"
INFRA = REPO_ROOT / "infra"
# Tablas de texto declaradas en el catálogo -> ruta bajo data/ (processed/<subdir>).
TEXT_VIEWS = ("text_docs", "term_freq", "text_ingest", "text_structure")


def _wf(name: str) -> dict:
    wf = yaml.safe_load((WF / name).read_text())
    wf["on"] = wf.pop(True, wf.get("on"))
    return wf


def _steps(name: str) -> list[dict]:
    return next(iter(_wf(name)["jobs"].values()))["steps"]


def test_ingesta_is_split_in_three_workflows_under_budget():
    for name in ("ingesta.yml", "ingesta-fuentes.yml", "ingesta-mercado.yml"):
        wf = _wf(name)
        assert wf["concurrency"] == {"group": "ingesta", "cancel-in-progress": False}
        job = next(iter(wf["jobs"].values()))
        assert job["timeout-minutes"] <= 330, name  # < 5 h 30
    fuentes = next(iter(_wf("ingesta-fuentes.yml")["jobs"].values()))
    assert int(fuentes["env"]["SOURCES_TOTAL_MIN"]) < 330
    run = " ".join(s.get("run", "") for s in fuentes["steps"])
    assert "sources_update.sh bls bea census" in run
    run_m = " ".join(s.get("run", "") for s in _steps("ingesta-mercado.yml"))
    assert "sources_update.sh cbp bds econcensus" in run_m


def test_only_fred_is_scheduled_and_chain_is_braked():
    assert "schedule" in _wf("ingesta.yml")["on"]
    for name in (
        "ingesta-fuentes.yml",
        "ingesta-mercado.yml",
        "ingesta-texto.yml",
        "detectores.yml",
    ):
        assert "schedule" not in _wf(name)["on"], name  # solo FRED arranca la semana
    expected = {
        "ingesta.yml": "ingesta-fuentes.yml",
        "ingesta-fuentes.yml": "ingesta-mercado.yml",
        "ingesta-mercado.yml": "ingesta-texto.yml",
        "ingesta-texto.yml": "detectores.yml",
        "detectores.yml": "reporte.yml",
    }
    for name, nxt in expected.items():
        wf = _wf(name)
        assert wf["permissions"].get("actions") == "write", name
        chain = [s for s in next(iter(wf["jobs"].values()))["steps"] if nxt in s.get("run", "")]
        assert chain, f"{name} no lanza {nxt}"
        assert "success()" in chain[-1]["if"], name  # freno: solo si todo salió bien
        assert "encadenar" in chain[-1]["if"], name
        assert (WF / nxt).exists()


def test_text_tables_are_pulled_and_synced():
    declared = {catalog.VIEWS[v][0] for v in TEXT_VIEWS}
    assert declared == {
        "text_docs",
        "term_freq",
        "lineage/text_ingest",
        "text_structure",
    }
    texto = (INFRA / "run_texto.sh").read_text()
    paths = texto[texto.index("TEXT_PATHS=(") : texto.index(")", texto.index("TEXT_PATHS=("))]
    pull = next(s["run"] for s in _steps("ingesta-texto.yml") if "pull_r2.sh" in s.get("run", ""))
    pull = re.sub(r"\s+", " ", pull)
    for sub in declared:
        assert f"processed/{sub}" in paths, f"{sub} falta en TEXT_PATHS"
        assert f"processed/{sub}" in pull, f"{sub} falta en el pull de ingesta-texto.yml"


def test_missing_structure_partition_forces_reprocess_of_current_schema():
    state = TextState.__new__(TextState)
    state.periods = {
        "2024-01": {"closed": True, "schema": 6, "docs": 10},
        "2024-02": {"closed": True, "schema": 6, "docs": 0},
    }
    now = datetime.now(UTC)
    assert not state.needs_run("2024-01", 20, now, min_schema=6)
    assert state.needs_run("2024-01", 20, now, min_schema=6, structure_missing=True)
    # un periodo sin documentos no tiene partición: no se reprocesa en cada corrida
    assert not state.needs_run("2024-02", 20, now, min_schema=6, structure_missing=True)
