"""Potencia y FDR del detector `regime` a escala (ADR 0034), con la tubería real.

Familia sintética que imita la corrida real: mezcla de frecuencias (A, M, Q), ruido AR(1) de
varias φ con un factor común entre series (correlación) y quiebres de media de distinto tamaño
(en sd del crecimiento). Se corre `run_family("regime", ...)` con la configuración de
`config.yaml` y se aplica BH como en `finalize`: una familia («antes») o una por frecuencia
(«después»).

Uso a escala completa (≈ 275.720 pruebas; minutos en 4 vCPU):
    uv run python -m tests.regime_scale --m 275720 --out tabla.json
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass

import numpy as np

from src.config import load_config
from src.detect.family import FamilyParams, Item, run_family
from src.stats.fdr import TailConfig, bh_adjust

Q = 0.05
# (grilla, periodos de crecimiento, persistencia) como en producción; peso = proporción del m real
# de la corrida del 2026-11-10 (A 70 %, M 15 %, Q 13 %; D y W se agregan a mensual).
GRIDS = {"A": (14, 2, 0.70), "M": (180, 6, 0.17), "Q": (60, 4, 0.13)}
SHIFTS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 12.0)
PHIS = (0.0, 0.3, 0.6, 0.9)


def params(escalate: int, escalate_max: int, workers: int = 4) -> FamilyParams:
    cfg = load_config()
    d, cal = cfg["detect"], cfg["fdr"]["calibration"]
    return FamilyParams(
        n_resamples=cal["n_resamples"],
        block_length=cal["block_length"],
        seed=cal["seed"],
        phi_margin=d["phi_margin"],
        regime_escalate=escalate,
        regime_escalate_max=escalate_max,
        tail=TailConfig(**d["tail"]),
        workers=workers,
    )


def _ar1(e: np.ndarray, phi: float) -> np.ndarray:
    x = np.empty_like(e)
    x[:, 0] = e[:, 0]
    for t in range(1, e.shape[1]):
        x[:, t] = phi * x[:, t - 1] + math.sqrt(1 - phi**2) * e[:, t]
    return x


def noise(rng: np.random.Generator, count: int, n: int, rho: float = 0.3) -> np.ndarray:
    """Ruido AR(1) (φ rotando en PHIS), sd marginal 1, con un factor común (correlación ρ)."""
    out = np.empty((count, n))
    common = _ar1(rng.standard_normal((1, n)), 0.5)
    for j, phi in enumerate(PHIS):
        rows = slice(j, count, len(PHIS))
        k = len(range(*rows.indices(count)))
        own = _ar1(rng.standard_normal((k, n)), phi)
        out[rows] = math.sqrt(1 - rho) * own + math.sqrt(rho) * common
    return out


def broken(rng: np.random.Generator, n: int, shift: float) -> np.ndarray:
    g = _ar1(rng.standard_normal((1, n)), 0.3)[0]
    g = g / g.std()
    g[n // 2 :] += shift
    return g


@dataclass
class Family:
    items: list[Item]
    freq: list[str]
    shift: list[float]  # 0 = ruido (nula verdadera)


def build(m: int, per_size: int, seed: int) -> Family:
    """Familia de `m` pruebas: ruido y `per_size` quiebres por tamaño y frecuencia."""
    rng = np.random.default_rng(seed)
    items: list[Item] = []
    freq: list[str] = []
    shift: list[float] = []
    for f, (n, k, w) in GRIDS.items():
        n_noise = max(int(m * w) - per_size * len(SHIFTS), 0)
        for g in noise(rng, n_noise, n):
            items.append(Item(g, 1.0, k, f if f != "A" else "A"))
            freq.append(f)
            shift.append(0.0)
        for s in SHIFTS:
            for _ in range(per_size):
                items.append(Item(broken(rng, n, s), 1.0, k, f))
                freq.append(f)
                shift.append(s)
    return Family(items, freq, shift)


def bh_flags(p: np.ndarray, groups: list[str] | None) -> np.ndarray:
    """BH sobre todo `p`, o por grupo si se pasan (familia por frecuencia)."""
    if groups is None:
        return bh_adjust(p) <= Q
    out = np.zeros(p.size, dtype=bool)
    g = np.array(groups)
    for key in np.unique(g):
        sel = g == key
        out[sel] = bh_adjust(p[sel]) <= Q
    return out


def evaluate(fam: Family, p: np.ndarray) -> dict:
    """FDR (sobre ruido = nula) y potencia por tamaño de quiebre, una familia o por frecuencia."""
    shift = np.array(fam.shift)
    res: dict = {}
    for name, groups in (("una_familia", None), ("por_frecuencia", fam.freq)):
        flags = bh_flags(p, groups)
        r = int(flags.sum())
        v = int((flags & (shift == 0)).sum())
        pw = {}
        for s in SHIFTS:
            sel = shift == s
            pw[str(s)] = float(flags[sel].mean())
        pw_by_f = {
            f: {
                str(s): float(flags[(np.array(fam.freq) == f) & (shift == s)].mean())
                for s in SHIFTS
            }
            for f in GRIDS
        }
        res[name] = {
            "n_bh": r,
            "falsos": v,
            "fdp": v / r if r else 0.0,
            "potencia": pw,
            "potencia_por_frecuencia": pw_by_f,
        }
    return res


def run(m: int, per_size: int, seed: int, escalate: int, escalate_max: int, workers: int) -> dict:
    fam = build(m, per_size, seed)
    t0 = time.time()
    p = np.empty(len(fam.items))
    methods: dict[str, int] = {}
    # Una llamada por frecuencia (grillas distintas), como `build_test_rows`: la longitud de
    # bloque "auto" sale de la mediana de cada subfamilia.
    for f in GRIDS:
        pos = [i for i, x in enumerate(fam.freq) if x == f]
        res, _ = run_family(
            "regime", [fam.items[i] for i in pos], params(escalate, escalate_max, workers)
        )
        for i, r in zip(pos, res, strict=True):
            p[i] = r.p_value
            methods[r.p_method] = methods.get(r.p_method, 0) + 1
    out = evaluate(fam, p)
    out["m"] = len(fam.items)
    out["segundos"] = time.time() - t0
    out["metodos_p"] = methods
    out["p_min"] = float(p.min())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--m", type=int, default=20_000)
    ap.add_argument("--per-size", type=int, default=20)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--escalate", type=int, default=99_999)
    ap.add_argument("--escalate-max", type=int, default=999_999)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out")
    a = ap.parse_args()
    out = run(a.m, a.per_size, a.seed, a.escalate, a.escalate_max, a.workers)
    text = json.dumps(out, indent=1)
    print(text)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)


if __name__ == "__main__":
    main()
