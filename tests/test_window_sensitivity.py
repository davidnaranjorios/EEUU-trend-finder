"""Prepared-snapshot harness tests loaded under a neutral name to preserve eval isolation."""

import copy
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.detect.family import FamilyParams
from src.stats.fdr import TailConfig
from src.stats.transform import grid_index

MODULE = runpy.run_path(str(Path(__file__).parents[1] / "eval" / "window_sensitivity.py"))


def snapshot(grids=("M",)):
    from datetime import date

    anchors = {"M": "2024-12-01", "Q": "2024-10-01", "A": "2024-01-01"}
    sizes = {"M": 180, "Q": 61, "A": 40}
    rng = np.random.default_rng(91)
    return {
        "schema_version": 1,
        "prepared_for_max_window_years": 7,
        "baseline_min_periods": 33,
        "input_manifest_sha256": "0" * 64,
        "cutoff": "2024-12-31",
        "anchors": {g: anchors[g] for g in grids},
        "items": [
            {
                "source": f"source-{i}",
                "object_id": f"synthetic-{i}",
                "grid": grid,
                "anchor": anchors[grid],
                "last_index": grid_index(date.fromisoformat(anchors[grid]), grid),
                "growth": rng.standard_normal(sizes[grid]).tolist(),
                "scale": 1.0,
                "persistence": 2,
            }
            for i, grid in enumerate(grids)
        ],
    }


def test_preparation_is_copied_immutable_and_minimums_enforced():
    source = snapshot(("M", "Q", "A"))
    pairs = MODULE["prepared_items"](source)
    source["items"][0]["growth"][0] = 900
    assert pairs[0][1].growth[0] != 900
    assert all(not item.growth.flags.writeable for _, item in pairs)
    for position in (1, 2):
        short = snapshot(("M", "Q", "A"))
        short["items"][position]["growth"].pop()
        with pytest.raises(ValueError, match="W7 history"):
            MODULE["prepared_items"](short)


@pytest.mark.parametrize(
    "fault", ["future", "anchor", "duplicate", "nan", "scale", "baseline", "hash"]
)
def test_invalid_declarations_rejected(fault):
    source = snapshot()
    if fault == "future":
        source["cutoff"] = "2024-11-01"
    elif fault == "anchor":
        source["items"][0]["anchor"] = "2024-11-01"
    elif fault == "duplicate":
        source["items"] *= 2
    elif fault == "nan":
        source["items"][0]["growth"][0] = float("nan")
    elif fault == "scale":
        source["items"][0]["scale"] = 0
    elif fault == "baseline":
        source["baseline_min_periods"] = 5
    else:
        source["input_manifest_sha256"] = "bad"
    with pytest.raises(ValueError):
        MODULE["prepared_items"](source)


def test_common_items_window_conversion_and_global_bh(monkeypatch):
    evaluate = MODULE["evaluate"]
    seen = []

    def fake(detector, items, params):
        assert detector == "slope"
        seen.extend((id(item), item.grid, params.window, item.growth.copy()) for item in items)
        p = 0.03 if items[0].grid == "M" else 0.06
        result = SimpleNamespace(p_value=p, p_method="empirical", effect=1, persistent=True)
        return [result] * len(items), {(i.grid, i.growth.size): 2.0 for i in items}

    monkeypatch.setitem(evaluate.__globals__, "run_family", fake)
    source = snapshot(("M", "Q"))
    before = copy.deepcopy(source)
    params = FamilyParams(n_resamples=99, seed=7, workers=1)
    result = evaluate(source, params)
    assert source == before and params.window == 60
    assert {(grid, window) for _, grid, window, _ in seen} == {
        ("M", 36),
        ("M", 60),
        ("M", 84),
        ("Q", 12),
        ("Q", 20),
        ("Q", 28),
    }
    for grid in ("M", "Q"):
        group = [row for row in seen if row[1] == grid]
        assert len({row[0] for row in group}) == 1
        assert all(np.array_equal(group[0][3], row[3]) for row in group)
    # Pooled BH is .06 for both; BH per grid would incorrectly discover the .03 item.
    assert all(
        not row["bh"] and row["q_bh"] == 0.06
        for rows in result["scenarios"].values()
        for row in rows
    )
    assert "unverified" in result["manifest"]["provenance"]
    source["items"].reverse()
    reordered = evaluate(source, params)
    assert reordered["scenarios"] == result["scenarios"]
    assert reordered["comparisons"] == result["comparisons"]


def test_empty_disjoint_and_opposite_sign_are_not_perfect_stability():
    compare = MODULE["compare"]
    assert compare([], [])["jaccard"] is None
    a = [{"identity": "a", "bh": True, "effect": 1}, {"identity": "b", "bh": False, "effect": 1}]
    b = [{"identity": "a", "bh": False, "effect": 1}, {"identity": "b", "bh": True, "effect": 1}]
    assert compare(a, b)["jaccard"] == 0
    assert compare(a, b)["sign_agreement_among_shared_signals"] is None
    b[0].update(bh=True, effect=-1)
    assert compare(a, b)["sign_agreement_among_shared_signals"] == 0
    with pytest.raises(ValueError, match="common unique universe"):
        compare(a, b[:1])


@pytest.mark.parametrize("kind", ["iid", "ar1", "factor", "positive"])
def test_controls_are_deterministic_and_smoke_is_not_certification(kind):
    controls = MODULE["controls"]
    growth = controls(kind, 3, 180, 17)
    assert np.array_equal(growth, controls(kind, 3, 180, 17))
    source = snapshot()
    template = source["items"][0]
    source["items"] = [
        dict(template, object_id=f"control-{i}", growth=g.tolist()) for i, g in enumerate(growth)
    ]
    params = FamilyParams(n_resamples=99, seed=7, workers=1, tail=TailConfig(method="none"))
    first = MODULE["evaluate"](source, params)
    second = MODULE["evaluate"](source, params)
    assert first == second
    assert "does not certify FDR" in first["manifest"]["calibration"]
    if kind == "positive":
        assert first["scenarios"]["7"][0]["bh"]


def test_cli_writes_only_new_output_and_preserves_snapshot(tmp_path):
    source = snapshot()
    inp = tmp_path / "prepared.json"
    out = tmp_path / "result.json"
    inp.write_text(json.dumps(source), encoding="utf-8")
    original = inp.read_bytes()
    main = MODULE["main"]
    assert main([str(inp), "--output", str(out), "--seed", "7", "--n-resamples", "19"]) == 0
    assert inp.read_bytes() == original
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["manifest"]["snapshot_sha256"] == MODULE["digest"](source)
    with pytest.raises(SystemExit):
        main([str(inp), "--output", str(out), "--seed", "7"])
    with pytest.raises(SystemExit):
        main([str(inp), "--output", str(inp), "--seed", "7"])
