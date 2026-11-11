"""Diagnóstico del detector `regime` (sin cambiar su calibración).

Corre la tubería real (`run_family` con la escalación de `config.yaml`) sobre datos sintéticos:
  1. ruido AR(1) puro: distribución de p, p mínimo alcanzado y método;
  2. quiebres de media de distinta fuerza (en desviaciones estándar): p alcanzado y método;
  3. con ese p, cuántas pruebas totales `m` admite BH (p_(k) <= q·k/m): el p mínimo alcanzable
     decide si 0 señales en m = 260.772 es una falla o un resultado legítimo.

Uso: uv run python -m eval.diagnostico_regime [--n-noise N] [--out FILE]
"""

from __future__ import annotations

import argparse
import json

import numpy as np

from src.config import load_config
from src.detect.family import FamilyParams, Item, run_family
from src.detect.numeric import cusum_stat
from src.stats.bootstrap import AR_BURN_IN, ar_null, fit_ar1, stationary_indices
from src.stats.fdr import TailConfig, bh_adjust


def ar1(rng: np.random.Generator, n: int, phi: float) -> np.ndarray:
    e = rng.normal(size=n + 50)
    x = np.zeros(n + 50)
    for t in range(1, n + 50):
        x[t] = phi * x[t - 1] + e[t]
    return x[50:]


def params(cfg: dict, workers: int = 1, escalate: int | None = None) -> FamilyParams:
    d = cfg["detect"]
    t = d["tail"]
    return FamilyParams(
        n_resamples=cfg["fdr"]["calibration"]["n_resamples"],
        block_length=cfg["fdr"]["calibration"]["block_length"],
        seed=cfg["fdr"]["calibration"]["seed"],
        phi_margin=d["phi_margin"],
        regime_escalate=d["regime_escalate"] if escalate is None else escalate,
        tail=TailConfig(**t),
        workers=workers,
    )


def summarize(res: list) -> dict:
    p = np.array([r.p_value for r in res])
    methods: dict[str, int] = {}
    for r in res:
        methods[r.p_method] = methods.get(r.p_method, 0) + 1
    return {
        "n": len(res),
        "p_min": float(p.min()),
        "p_q01": float(np.quantile(p, 0.01)),
        "p_median": float(np.median(p)),
        "frac_p_lt_0.05": float((p < 0.05).mean()),
        "methods": methods,
    }


def oracle_p(
    g: np.ndarray, pos: int, k: int, cfg: dict, b: int = 4999
) -> tuple[float, float, float]:
    """(T_obs, p con nulo de la serie CON quiebre, p con nulo de la serie SIN quiebre).

    Misma φ, mismo estadístico y mismos índices; la única diferencia es si los residuos que se
    remuestrean todavía contienen el escalón (como hace la tubería) o ya no (oráculo: se resta
    la media de cada tramo con la posición verdadera del quiebre)."""
    n = g.size
    phi = fit_ar1(g, cfg["detect"]["phi_margin"])
    idx = stationary_indices(n, b, 2.0, (1, 2, 3), n_out=n + AR_BURN_IN)
    t_obs = float(cusum_stat(g, k)[0])
    t_with = cusum_stat(ar_null(g, idx, phi), k)[0]
    flat = g.copy()
    flat[:pos] -= g[:pos].mean()
    flat[pos:] -= g[pos:].mean()
    t_flat = cusum_stat(ar_null(flat, idx, phi), k)[0]
    pw = (1 + np.count_nonzero(t_with >= t_obs)) / (b + 1)
    pf = (1 + np.count_nonzero(t_flat >= t_obs)) / (b + 1)
    return t_obs, pw, pf


def bh_count(p_signal: np.ndarray, m: int, q: float = 0.05) -> int:
    """Señales BH si hay `m` pruebas: las de `p_signal` más el resto nulo (p = 1, el caso
    más favorable para el detector: el resto no compite por rangos)."""
    p = np.concatenate([p_signal, np.ones(m - p_signal.size)])
    return int((bh_adjust(p) <= q).sum())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-noise", type=int, default=300)
    ap.add_argument("--n-break", type=int, default=20)
    ap.add_argument("--n", type=int, default=60, help="periodos (trimestral: 15 años)")
    ap.add_argument("--escalate", type=int, help="réplicas de escalación (por defecto: config)")
    ap.add_argument("--shifts", default="1,2,3,5", help="fuerzas del quiebre, en sd")
    ap.add_argument("--out")
    a = ap.parse_args()
    cfg = load_config()
    k = 4  # persistencia trimestral
    rng = np.random.default_rng(11)
    out: dict = {
        "n_periods": a.n,
        "B": cfg["fdr"]["calibration"]["n_resamples"],
        "escalate": a.escalate or cfg["detect"]["regime_escalate"],
    }
    # UNA familia (como en la corrida real): ruido AR(1) mayoritario + quiebres de distinta
    # fuerza. La longitud de bloque "auto" sale de la mediana de toda la familia.
    items: list[Item] = []
    labels: list[str] = []
    for phi in (0.0, 0.5):
        for _ in range(a.n_noise):
            items.append(Item(ar1(rng, a.n, phi), 1.0, k, "Q"))
            labels.append(f"ruido_phi{phi}")
    shifts = tuple(float(x) for x in a.shifts.split(","))
    for shift in shifts:
        for _ in range(a.n_break):
            g = ar1(rng, a.n, 0.3)
            g = g / g.std()
            g[a.n // 2 :] += shift
            items.append(Item(g, 1.0, k, "Q"))
            labels.append(f"quiebre_{shift}sd")
    res, blocks = run_family("regime", items, params(cfg, escalate=a.escalate))
    out["block_length"] = {f"{g}|{n}": float(v) for (g, n), v in blocks.items()}
    for lab in dict.fromkeys(labels):
        sel = [r for r, name in zip(res, labels, strict=True) if name == lab and r is not None]
        s = summarize(sel)
        if lab.startswith("quiebre"):
            its = [it for it, name in zip(items, labels, strict=True) if name == lab]
            orc = np.array([oracle_p(it.growth, a.n // 2, k, cfg) for it in its])
            pv = np.array([r.p_value for r in sel])
            s["T_obs_median"] = float(np.median(orc[:, 0]))
            s["p_nulo_con_quiebre_mediana"] = float(np.median(orc[:, 1]))
            s["p_nulo_sin_quiebre_mediana"] = float(np.median(orc[:, 2]))
            s["bh_m_260772"] = bh_count(pv, 260_772)
            s["bh_m_48699"] = bh_count(pv, 48_699)
        out[lab] = s
    text = json.dumps(out, indent=1, ensure_ascii=False)
    print(text)
    if a.out:
        open(a.out, "w").write(text)


if __name__ == "__main__":
    main()
