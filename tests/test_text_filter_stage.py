"""ADR 0035: los filtros de amplitud y de largo de segmento pueden aplicarse a los RESULTADOS.

- con `filter_stage.breadth = after` el término angosto entra a la familia (m sube) y se prueba;
  la señal se descarta después de BH, y el reporte guarda los conteos BH/BY antes del filtro;
- el filtro de amplitud no mira el estadístico: sobre ruido AR(1) con factor común, aplicarlo
  después de BH nunca agrega descubrimientos y la FDR queda dentro de lo esperado por q;
- el modo por defecto (`before`) no cambia nada.
"""

from __future__ import annotations

import copy
from datetime import datetime

import numpy as np
import pytest

from src.config import load_config
from src.detect.text import TermMatrix, detect_source, post_filter_mask
from src.stats.fdr import bh_adjust
from tests.test_detect_text import YEARS, null_family_pvalues, null_logits, write_term_freq


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"]["processed_dir"] = str(tmp_path / "processed")
    c["paths"]["signals_dir"] = str(tmp_path / "signals")
    c["fdr"]["calibration"]["n_resamples"] = 499
    c["text_detect"]["workers"] = 1
    c["text_detect"]["min_mean_share"] = 0.005
    c["text_detect"]["min_entities"] = {"edgar": 2}
    return c


def _write(tmp_path):
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
    )


def test_breadth_after_tests_the_narrow_term_but_drops_the_signal(cfg, tmp_path):
    _write(tmp_path)
    before = detect_source(cfg, "edgar", "r-before", datetime.now())
    assert before["n_tests"] == 41 and before["prefiltro"]["excluidos_amplitud"] == 1

    cfg["text_detect"]["filter_stage"] = {"breadth": "after"}
    after = detect_source(cfg, "edgar", "r-after", datetime.now())
    ids = {oid for oid, _, _ in after["top"]}
    assert after["n_tests"] == 42  # la familia incluye al término angosto
    assert "item_1a|amplio nuevo" in ids and "item_1a|estrecho nuevo" not in ids
    assert after["n_bh_raw"] == after["n_bh"] + 1  # el angosto pasa BH y se descarta después
    assert after["n_by_raw"] >= after["n_by"]


def test_default_stage_is_unchanged(cfg, tmp_path):
    _write(tmp_path)
    a = detect_source(cfg, "edgar", "r-a", datetime.now())
    cfg["text_detect"]["filter_stage"] = {"breadth": "before", "persistence": "before"}
    b = detect_source(cfg, "edgar", "r-b", datetime.now())
    assert (a["n_tests"], a["n_bh"], a["n_by"]) == (b["n_tests"], b["n_bh"], b["n_by"])


def test_post_filter_mask_keeps_unmeasured_and_enforces_length():
    m = TermMatrix(
        [],
        [("s", "a"), ("s", "b"), ("s", "c")],
        np.zeros((3, 0)),
        np.zeros((3, 0)),
        {},
        entities=np.array([1.0, 30.0, np.nan]),
        industries=np.array([np.nan, np.nan, np.nan]),
    )
    res = [(1.0, 0.01, "empirical", 8), (1.0, 0.01, "empirical", 3), (1.0, 0.01, "empirical", 8)]
    ok = post_filter_mask(m, res, min_entities=20, min_industries=0, min_length=8)
    assert ok.tolist() == [False, False, True]  # a: angosto; b: segmento corto; c: sin medida
    assert post_filter_mask(m, res, 0, 0, 0).all()


def test_fdr_on_noise_does_not_inflate_when_breadth_is_applied_after():
    """Ruido AR(1) (φ=0,7) con factor común, 40 familias de 80 términos y una amplitud al azar
    (independiente del nulo): BH sobre la familia completa y filtro después. P(algún rechazo)
    queda <= q + holgura y el filtro solo puede quitar rechazos, nunca agregarlos."""
    rng = np.random.default_rng(3)
    any_rej, kept = [], []
    for seed in range(40):
        p = null_family_pvalues(seed, 0.7)
        rej = bh_adjust(p) <= 0.05
        wide = rng.random(p.size) < 0.6
        any_rej.append(rej.any())
        kept.append((rej & wide).any())
        assert (rej & wide).sum() <= rej.sum()
    assert np.mean(any_rej) <= 0.12 and np.mean(kept) <= np.mean(any_rej)
