"""Fase 3, parte B: detector de términos emergentes (criterio de terminado de CLAUDE.md).

- un término sintético con crecimiento artificial inyectado se detecta (q <= 0,05);
- un salto aislado no genera señal (persistencia);
- bajo el nulo (proporciones con AR(1) φ=0,7 y correlación entre términos) la FDR queda
  dentro de q y el tamaño de la prueba no se infla;
- detector_runs trae el conteo BH y el BY de comparación;
- el prefiltro de palabras funcionales sale de los datos (sin listas).
"""

from __future__ import annotations

import copy
import json

import numpy as np
import pyarrow as pa
import pytest

from src.config import load_config
from src.detect.text import (
    DETECTOR,
    TermTestParams,
    detect_source,
    emerging_stat,
    logit_share,
    run_family,
)
from src.ingest import store
from src.schemas import DETECTOR_RUNS, SIGNALS, TERM_FREQ
from src.stats.fdr import TailConfig, bh_adjust

YEARS = list(range(2000, 2024))  # 24 años (grilla anual de EDGAR)
Q_DOCS = (2000, 600, 300, 300)  # documentos por trimestre (Q1 concentra, como en EDGAR)


def null_logits(rng, k: int, g: int, phi: float = 0.7, rho: float = 0.5, sd: float = 0.15):
    """Logit latente AR(1) por término con un factor común (correlación entre términos)."""
    common = np.zeros(g)
    own = np.zeros((k, g))
    for t in range(1, g):
        common[t] = phi * common[t - 1] + rng.normal(0, sd)
        own[:, t] = phi * own[:, t - 1] + rng.normal(0, sd, k)
    level = rng.uniform(np.log(0.01 / 0.99), np.log(0.2 / 0.8), (k, 1))
    return level + np.sqrt(rho) * common + np.sqrt(1 - rho) * own


def to_counts(rng, logits: np.ndarray, totals: np.ndarray) -> np.ndarray:
    p = 1 / (1 + np.exp(-logits))
    return rng.binomial(totals.astype(int), p)


def write_term_freq(
    processed, source, section, shares_by_term, min_doc_count, rng, entities=None, spikes=None
):
    """term_freq trimestral sintético: share anual por término, binomial por trimestre.

    `entities` (término -> n): amplitud fija por término; por defecto, una entidad por documento.
    """
    entities = entities or {}
    spikes = spikes or {}
    for yi, year in enumerate(YEARS):
        for q, n in enumerate(Q_DOCS, start=1):
            rows = []
            for term, shares in shares_by_term.items():
                c = int(rng.binomial(n, shares[yi]))
                if c >= min_doc_count:  # censura de la ingesta
                    rows.append(
                        {
                            "source": source,
                            "section": section,
                            "period": f"{year}Q{q}",
                            "term": term,
                            "doc_count": c,
                            "total_docs": n,
                            "n_entities": min(
                                spikes.get(term, {}).get((year, q), entities.get(term, c)), c
                            ),
                        }
                    )
            store.replace_partition(
                pa.Table.from_pylist(rows, schema=TERM_FREQ.schema),
                TERM_FREQ,
                processed / "term_freq",
                source=source,
                partition=f"{year}Q{q}",
            )


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"]["processed_dir"] = str(tmp_path / "processed")
    c["paths"]["signals_dir"] = str(tmp_path / "signals")
    c["fdr"]["calibration"]["n_resamples"] = 499
    c["text_detect"]["workers"] = 1
    c["text_detect"]["min_mean_share"] = 0.005
    return c


def test_injected_term_detected_and_isolated_jump_ignored(cfg, tmp_path):
    rng = np.random.default_rng(7)
    g = len(YEARS)
    shares = {}
    for i, lg in enumerate(null_logits(rng, 60, g)):
        shares[f"ruido{i} palabra"] = 1 / (1 + np.exp(-lg))
    base = np.full(g, 0.01)
    inj = base.copy()
    inj[-3:] = [0.03, 0.06, 0.10]  # crecimiento artificial sostenido
    shares["inyectado nuevo"] = inj
    jump = np.full(g, 0.05)
    jump[-1] = 0.30  # salto aislado en el último periodo
    shares["salto aislado"] = jump
    mid = np.full(g, 0.05)
    mid[10] = 0.40  # salto aislado en la historia
    shares["salto medio"] = mid
    shares["aa"] = np.full(g, 0.95)  # palabra funcional (casi omnipresente)
    shares["aa inyectado"] = inj  # empieza con ella: el prefiltro la excluye
    write_term_freq(tmp_path / "processed", "edgar", "item_1a", shares, 5, rng)

    report = detect_source(cfg, "edgar", "run-test", __import__("datetime").datetime.now())
    assert report["grid"] == "A" and report["periods"] == g
    found = {oid: q for oid, _, q in report["top"]}
    assert "item_1a|inyectado nuevo" in found and found["item_1a|inyectado nuevo"] <= 0.05
    assert "item_1a|salto aislado" not in found
    assert "item_1a|salto medio" not in found
    assert report["prefiltro"]["palabras_funcionales"] == 1
    signals = store.read_table(SIGNALS, tmp_path / "signals", source="edgar").to_pylist()
    assert {s["object_id"] for s in signals} == set(found)
    assert all(s["detector"] == DETECTOR and s["n_tests"] == report["n_tests"] for s in signals)
    assert not any("aa" in s["object_id"].split("|")[1].split() for s in signals)

    runs = store.read_table(
        DETECTOR_RUNS, tmp_path / "processed" / "lineage" / "detector_runs", source="edgar"
    ).to_pylist()
    assert len(runs) == 1
    r = runs[0]
    assert (r["n_tests"], r["n_bh"], r["n_by"]) == (
        report["n_tests"],
        report["n_bh"],
        report["n_by"],
    )
    assert r["n_by"] <= r["n_bh"]  # BY es más estricto: solo comparación
    params = json.loads(r["params_json"])
    assert params["grid"] == "A" and params["window"] == 7 and params["persistence"] == 2
    assert params["recent"] == 4


def test_breadth_filter_excludes_single_entity_terms(cfg, tmp_path):
    """Mismo crecimiento en dos términos: el que usa una sola entidad (p. ej. el nombre de quien
    firma los avisos de una agencia) no entra a la familia con min_entities = 2."""
    rng = np.random.default_rng(11)
    g = len(YEARS)
    shares = {
        f"ruido{i} palabra": 1 / (1 + np.exp(-lg)) for i, lg in enumerate(null_logits(rng, 40, g))
    }
    inj = np.full(g, 0.01)
    inj[-3:] = [0.03, 0.06, 0.10]
    shares["amplio nuevo"] = inj
    shares["estrecho nuevo"] = inj.copy()
    write_term_freq(
        tmp_path / "processed",
        "edgar",
        "item_1a",
        shares,
        5,
        rng,
        entities={"estrecho nuevo": 1},
        # Un trimestre suelto con 3 entidades no lo vuelve amplio (mediana, no máximo).
        spikes={"estrecho nuevo": {(YEARS[-1], 2): 3}},
    )
    cfg["text_detect"]["min_entities"] = {"edgar": 2}
    report = detect_source(cfg, "edgar", "run-amp", __import__("datetime").datetime.now())
    found = {oid for oid, _, _ in report["top"]}
    assert "item_1a|amplio nuevo" in found
    assert "item_1a|estrecho nuevo" not in found
    assert report["prefiltro"]["excluidos_amplitud"] == 1
    assert report["n_tests"] == 41


def test_statistic_requires_persistence():
    x = np.zeros(12)
    x[-1] = 5.0  # un solo periodo elevado
    assert emerging_stat(x, 7, 2, 4) == pytest.approx(0.0)
    x[-2] = 3.0
    assert emerging_stat(x, 7, 2, 4) == pytest.approx(3.0)
    x[-4] = 9.0  # dentro de la ventana reciente: no entra a la comparación
    assert emerging_stat(x, 7, 2, 4) == pytest.approx(3.0)


def null_family_pvalues(seed: int, phi: float = 0.7, k: int = 80):
    rng = np.random.default_rng(seed)
    g = len(YEARS)
    totals = np.tile(np.full(g, float(sum(Q_DOCS))), (k, 1))
    x = logit_share(to_counts(rng, null_logits(rng, k, g, phi=phi), totals), totals)
    margin = load_config()["text_detect"]["phi_margin"]
    params = TermTestParams(7, 2, 4, margin, TailConfig())
    res, _ = run_family(x, params, 499, "auto", seed, "edgar", workers=1)
    return np.array([r[1] for r in res])


@pytest.mark.parametrize("phi", [0.7, 0.9])
def test_fdr_and_size_under_correlated_ar1_null(phi):
    """Nulo completo con AR(1) y un factor común entre términos (ρ=0,5), en la grilla anual
    de EDGAR (24 años, la más corta): FDR = P(al menos un rechazo de BH) cerca de q o debajo, y
    tamaño al 5 % sin inflar. 40 familias de 80 términos."""
    reps = [null_family_pvalues(seed, phi) for seed in range(40)]
    any_rejection = np.mean([np.any(bh_adjust(p) <= 0.05) for p in reps])
    size = np.mean(np.concatenate(reps) <= 0.05)
    # 40 familias: error estándar ~0,034 alrededor de 0,05; tope con ~2 se de holgura.
    assert any_rejection <= 0.12, any_rejection
    assert size <= 0.06, size
