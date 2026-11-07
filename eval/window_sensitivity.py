"""Local sensitivity of prepared numeric snapshots; not a backtest or ranking evaluation.

The producer must prepare each series once for W7, without data after cutoff. This module
checks structure and declared anchors, but cannot prove provenance from growth arrays alone.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import platform
from dataclasses import asdict, replace
from datetime import date
from importlib.metadata import version
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

from src.detect.family import FamilyParams, Item, run_family
from src.stats.fdr import TailConfig, bh_adjust, by_adjust
from src.stats.transform import PERIODS_PER_YEAR, grid_index

WINDOW_YEARS = (3, 5, 7)
BASELINE_MIN_PERIODS = 33


def digest(value: Any) -> str:
    """SHA256 of canonical JSON, rejecting nonfinite numeric values."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def prepared_items(snapshot: dict[str, Any]) -> list[tuple[str, Item]]:
    """Validate declarations and copy arrays, sorted by stable source/object identity."""
    if snapshot.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    if snapshot.get("prepared_for_max_window_years") != 7:
        raise ValueError("prepared_for_max_window_years must be 7")
    if snapshot.get("baseline_min_periods") != BASELINE_MIN_PERIODS:
        raise ValueError("baseline_min_periods must be 33; never lower it to admit series")
    source_hash = snapshot.get("input_manifest_sha256", "")
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(c not in "0123456789abcdef" for c in source_hash)
    ):
        raise ValueError("input_manifest_sha256 must identify the producer manifest")
    cutoff = date.fromisoformat(snapshot["cutoff"])
    anchors = snapshot["anchors"]
    if not isinstance(anchors, dict) or set(anchors) - set(PERIODS_PER_YEAR):
        raise ValueError("anchors must contain only M/Q/A")
    for anchor in anchors.values():
        if date.fromisoformat(anchor) > cutoff:
            raise ValueError("anchor after cutoff")
    rows = snapshot["items"]
    if not isinstance(rows, list):
        raise ValueError("items must be a list")
    found = {}
    for row in rows:
        if not isinstance(row.get("source"), str) or not row["source"]:
            raise ValueError("source must be nonempty")
        if not isinstance(row.get("object_id"), str) or not row["object_id"]:
            raise ValueError("object_id must be nonempty")
        key = json.dumps([row["source"], row["object_id"]], separators=(",", ":"))
        if key in found:
            raise ValueError("duplicate source/object_id")
        grid = row["grid"]
        if grid not in PERIODS_PER_YEAR or grid not in anchors:
            raise ValueError("missing or unsupported grid anchor")
        if row["anchor"] != anchors[grid]:
            raise ValueError("items in a grid must share exactly the declared anchor")
        # The index comparison makes the declared grid alignment explicit.
        if row["last_index"] != grid_index(date.fromisoformat(row["anchor"]), grid):
            raise ValueError("last_index does not match anchor")
        g = np.array(row["growth"], dtype=float, copy=True)
        maximum = 7 * PERIODS_PER_YEAR[grid]
        if g.ndim != 1 or g.size < maximum + max(maximum, BASELINE_MIN_PERIODS):
            raise ValueError("insufficient W7 history or one-dimensional growth required")
        if not np.isfinite(g).all():
            raise ValueError("growth must be finite")
        scale = row["scale"]
        if not isinstance(scale, (int, float)) or not np.isfinite(scale) or scale <= 0:
            raise ValueError("scale must be finite and positive")
        persistence = row["persistence"]
        if type(persistence) is not int or not 0 < persistence <= 3 * PERIODS_PER_YEAR[grid]:
            raise ValueError("persistence must fit the shortest window")
        g.flags.writeable = False
        found[key] = Item(g, float(scale), persistence, grid)
    return sorted(found.items())


def compare(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare discoveries on exactly the same universe; empty is not perfect stability."""
    a, b = {r["identity"]: r for r in left}, {r["identity"]: r for r in right}
    if len(a) != len(left) or len(b) != len(right) or a.keys() != b.keys():
        raise ValueError("comparison requires a common unique universe")
    sa = {key for key, row in a.items() if row["bh"]}
    sb = {key for key, row in b.items() if row["bh"]}
    common, union = sa & sb, sa | sb
    same_sign = sum(np.sign(a[k]["effect"]) == np.sign(b[k]["effect"]) for k in common)
    return {
        "n_items": len(a),
        "left_signals": len(sa),
        "right_signals": len(sb),
        "intersection": len(common),
        "union": len(union),
        "jaccard": len(common) / len(union) if union else None,
        "left_retention": len(common) / len(sa) if sa else None,
        "right_retention": len(common) / len(sb) if sb else None,
        "sign_agreement_among_shared_signals": same_sign / len(common) if common else None,
        "left_only": sorted(sa - sb),
        "right_only": sorted(sb - sa),
    }


def evaluate(snapshot: dict[str, Any], params: FamilyParams, q: float = 0.05) -> dict[str, Any]:
    """Run three slope scenarios; BH pools all grids/sources within each scenario."""
    if not 0 < q < 1:
        raise ValueError("q must be between 0 and 1")
    if params.n_resamples < 1 or params.workers < 1:
        raise ValueError("positive resamples and workers required")
    pairs = prepared_items(snapshot)
    scenarios = {}
    for years in WINDOW_YEARS:
        rows = []
        for grid in sorted({item.grid for _, item in pairs}):
            group = [(key, item) for key, item in pairs if item.grid == grid]
            p = replace(params, window=years * PERIODS_PER_YEAR[grid])
            results, blocks = run_family("slope", [item for _, item in group], p)
            for (key, item), result in zip(group, results, strict=True):
                if result is None:
                    raise RuntimeError("unexpected untested item")
                rows.append(
                    {
                        "identity": key,
                        "grid": grid,
                        "years_recent": years,
                        "years_prior": years,
                        "window_periods": p.window,
                        "n_growth": item.growth.size,
                        "n_null_baseline": item.growth.size - p.window,
                        "p": result.p_value,
                        "p_method": result.p_method,
                        "effect": result.effect,
                        "persistent": result.persistent,
                        "block_length": blocks[(grid, item.growth.size)],
                    }
                )
        rows.sort(key=lambda row: row["identity"])
        bh = bh_adjust(np.array([r["p"] for r in rows]))
        by = by_adjust(np.array([r["p"] for r in rows]))
        for row, qb, qy in zip(rows, bh, by, strict=True):
            row.update(q_bh=float(qb), q_by=float(qy), bh=bool(qb <= q), by=bool(qy <= q))
        scenarios[str(years)] = rows
    return {
        "manifest": {
            "snapshot_sha256": digest(snapshot),
            "input_manifest_sha256": snapshot["input_manifest_sha256"],
            "cutoff": snapshot["cutoff"],
            "anchors": copy.deepcopy(snapshot["anchors"]),
            "params": asdict(params),
            "window_override": "params.window replaced by years * periods_per_year per row",
            "versions": {name: version(name) for name in ("numpy", "scipy")},
            "python": platform.python_version(),
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "q": q,
            "universe": [key for key, _ in pairs],
            "scope": "prepared snapshot slope sensitivity; not backtest or full-rank stability",
            "provenance": (
                "producer declarations; raw preparation, closed-period anchors and absence "
                "of future data unverified"
            ),
            "calibration": (
                "this run alone does not certify FDR; small-bootstrap runs are smoke only"
            ),
        },
        "scenarios": scenarios,
        "comparisons": {
            f"{a}-{b}": compare(scenarios[str(a)], scenarios[str(b)])
            for a, b in combinations(WINDOW_YEARS, 2)
        },
    }


def controls(kind: str, n_series: int, n_growth: int, seed: int) -> np.ndarray:
    """Deterministic growth controls; positive is a synthetic sustained change in series 0."""
    if n_series < 1 or n_growth < 168:
        raise ValueError("controls need positive series count and at least 168 growth periods")
    rng = np.random.default_rng(seed)
    e = rng.standard_normal((n_series, n_growth))
    if kind == "iid":
        return e
    if kind == "positive":
        e[0, -84:] += 4.0
        return e
    if kind not in ("ar1", "factor"):
        raise ValueError("unsupported control")

    def ar(values: np.ndarray, phi: float) -> np.ndarray:
        out = values.copy()
        for t in range(1, n_growth):
            out[:, t] = phi * out[:, t - 1] + np.sqrt(1 - phi**2) * values[:, t]
        return out

    if kind == "ar1":
        return ar(e, 0.7)
    common = ar(rng.standard_normal((1, n_growth)), 0.5)
    return (ar(e, 0.5) + common) / np.sqrt(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "snapshot", type=Path, help="explicit prepared snapshot JSON, not raw Parquet"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--n-resamples", type=int, default=999)
    parser.add_argument("--q", type=float, default=0.05)
    args = parser.parse_args(argv)
    if args.snapshot.resolve() == args.output.resolve():
        parser.error("output must not overwrite input snapshot")
    if args.output.exists():
        parser.error("output already exists; choose a new evaluation artifact")
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    result = evaluate(
        snapshot,
        FamilyParams(n_resamples=args.n_resamples, seed=args.seed, workers=1, tail=TailConfig()),
        args.q,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
