import copy
import json

import pytest

from src.config import ConfigError, load_config, validate_config
from src.log import run


def test_config_loads_with_defaults():
    cfg = load_config()
    assert cfg["fdr"]["q"] == 0.05
    assert cfg["fdr"]["method"] == "bh"
    assert cfg["fdr"]["comparison_method"] == "by"
    assert cfg["fdr"]["calibration"]["method"] == "stationary_bootstrap"
    assert cfg["fdr"]["calibration"]["shared_blocks_across_series"] is True


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("fdr", "q"), 1.5),
        (("fdr", "method"), "by"),  # BY no puede decidir el ranking
        (("fdr", "calibration", "method"), "permutation"),  # permutación simple prohibida
        (("fdr", "calibration", "block_length"), 1),
        (("eligibility", "min_obs"), {"M": 10}),
        (("exports", "groups"), ["favoritas"]),
        (("exports", "max_file_kb"), 0),
        (("exports", "max_total_mb"), -1),
        (("exports", "n_buckets"), 101),
        (("rank", "min_sources"), 1),  # triangulación: nunca una sola fuente
        (("rank", "weights"), {"strength": -1.0}),
        (("rank", "embeddings", "backend"), "remoto"),
        (("detect", "sources"), []),
        (("sources", "bea", "crawl_budget_share"), 0),
        (("sources", "bls", "crawl_budget_share"), 1.5),
    ],
)
def test_invalid_config_rejected(path, value):
    cfg = copy.deepcopy(load_config())
    node = cfg
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_run_record_written(tmp_path):
    runs_file = tmp_path / "logs" / "runs.jsonl"
    with run(["fred"], runs_file) as rec:
        with rec.stage("ingest"):
            rec.n_series["fred"] = 3
    record = json.loads(runs_file.read_text().strip())
    assert record["status"] == "ok"
    assert record["sources"] == ["fred"]
    assert record["n_series"] == {"fred": 3}
    assert "ingest" in record["stage_seconds"]
    assert {"run_id", "started_at", "finished_at", "total_seconds"} <= record.keys()


def test_failed_run_is_recorded(tmp_path):
    runs_file = tmp_path / "runs.jsonl"
    with pytest.raises(RuntimeError), run(["fred"], runs_file):
        raise RuntimeError("boom")
    assert json.loads(runs_file.read_text())["status"] == "failed"
