"""Principio 2: ningún módulo de src/ importa ni lee nada de eval/."""

import importlib
import pkgutil
import sys

import pytest

import src
from src.config import REPO_ROOT
from tests.guards import eval_access_violations

SRC = REPO_ROOT / "src"


def test_src_never_touches_eval():
    violations = eval_access_violations(SRC)
    assert violations == [], "\n".join(violations)


def test_importing_src_never_loads_eval():
    for mod in pkgutil.walk_packages(src.__path__, prefix="src."):
        importlib.import_module(mod.name)
    loaded = [m for m in sys.modules if m == "eval" or m.startswith("eval.")]
    assert loaded == []


TRAPS = {
    "import_eval.py": "import eval\n",
    "import_submodule.py": "import eval.backtest\n",
    "from_eval.py": "from eval import backtest\n",
    "from_submodule.py": "from eval.backtest import run\n",
    "dynamic_import.py": "import importlib\nimportlib.import_module('eval')\n",
    "path_literal.py": "open('eval/known_events.yaml')\n",
    "path_segment.py": "from pathlib import Path\nPath('.') / 'eval' / 'x.yaml'\n",
    "known_events_name.py": "NAME = 'known_events'\n",
    "query.sql": "SELECT * FROM read_yaml('eval/known_events.yaml')\n",
}


@pytest.mark.parametrize("filename", sorted(TRAPS))
def test_guard_catches_trap(tmp_path, filename):
    (tmp_path / filename).write_text(TRAPS[filename], encoding="utf-8")
    assert eval_access_violations(tmp_path), f"la trampa {filename} no fue detectada"


def test_guard_ignores_unrelated_names(tmp_path):
    (tmp_path / "ok.py").write_text(
        "evaluation = 1\nretrieval = 'evaluate'\nfrom src import config\n", encoding="utf-8"
    )
    assert eval_access_violations(tmp_path) == []
