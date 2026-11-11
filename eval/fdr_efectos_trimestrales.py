"""FDR de `rate_effect.evaluate` bajo ruido con la exigencia W + max(W, línea base) (ADR 0023).

Ruido puro: crecimiento AR(1) (φ 0,3 / 0,7 / 0,9) con un factor común entre clusters (correlación
entre series) y una tasa independiente (cambios AR(1) φ=0,5; nivel AR(1) φ=0,95). Todo lo que se
marque es falso positivo. Por familia (K clusters) se cuenta si hay al menos un rechazo:
  - `related`: q_bh <= q y q_diff <= q (relación con la tasa, en niveles Y diferencias);
  - `marked`: `rate_effect` (la etiqueta final).
Con todo nulo, P(>= 1 rechazo) es la FDR; debe quedar <= q.

Uso: uv run python -m eval.fdr_efectos_trimestrales [--reps R] [--k K] [--out F]
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np

from src.config import load_config
from src.rank.rate_effect import evaluate, params_from


def ar1_matrix(rng: np.random.Generator, k: int, n: int, phi: float, rho: float) -> np.ndarray:
    common = np.zeros(n + 50)
    own = np.zeros((k, n + 50))
    for t in range(1, n + 50):
        common[t] = phi * common[t - 1] + rng.normal()
        own[:, t] = phi * own[:, t - 1] + rng.normal(size=k)
    x = np.sqrt(rho) * common + np.sqrt(1 - rho) * own
    return x[:, 50:] / x[:, 50:].std(axis=1, keepdims=True)


def rate_maps(rng: np.random.Generator, n: int, last: int, lag: int = 3) -> tuple[dict, dict]:
    m = n + lag + 5
    d = np.zeros(m)
    lv = np.zeros(m)
    for t in range(1, m):
        d[t] = 0.5 * d[t - 1] + rng.normal()
        lv[t] = 0.95 * lv[t - 1] + rng.normal(scale=0.3)
    idx = [last - (m - 1) + i for i in range(m)]
    return dict(zip(idx, d, strict=True)), dict(zip(idx, lv, strict=True))


def one(cfg: dict, grid: str, n: int, phi: float, k: int, reps: int, seed: int) -> dict:
    p = params_from(cfg)
    pers = cfg["persistence"]["min_consecutive_periods"][grid]
    rng = np.random.default_rng(seed)
    rel = mark = ev = 0
    ntest = 0
    for _ in range(reps):
        g = ar1_matrix(rng, k, n, phi, 0.5)
        last = 1000
        dr, lv = rate_maps(rng, n, last)
        clusters = {f"c{i}": (g[i], last, grid, 1.0, pers) for i in range(k)}
        res = evaluate(clusters, {grid: dr}, p, {grid: lv})
        ev += sum(r.evaluated for r in res.values())
        ntest += sum(r.evaluated for r in res.values()) > 0
        rel += any(r.evaluated and r.q_bh <= p.q and r.q_diff <= p.q for r in res.values())
        mark += any(r.rate_effect for r in res.values())
    return {
        "grid": grid,
        "n": n,
        "phi": phi,
        "K": k,
        "reps": reps,
        "evaluados_por_familia": ev / reps,
        "P_alguna_relacion": rel / reps,
        "P_alguna_marca": mark / reps,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=100)
    ap.add_argument("--k", type=int, default=60)
    ap.add_argument("--out")
    a = ap.parse_args()
    cfg = load_config()
    out = []
    t0 = time.time()
    for grid, n in (("Q", 60), ("Q", 53), ("M", 130)):
        for phi in (0.3, 0.7, 0.9):
            r = one(cfg, grid, n, phi, a.k, a.reps, seed=7)
            r["segundos"] = round(time.time() - t0)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            out.append(r)
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
