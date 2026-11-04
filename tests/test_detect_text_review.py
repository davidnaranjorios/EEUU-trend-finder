"""Revisión de la corrida 2a02c981 (parte C): quiebre estimado, deriva, especificidad, amplitud."""

from __future__ import annotations

import copy
from datetime import date, datetime

import numpy as np
import pyarrow as pa
import pytest

from src.config import load_config
from src.detect.text import detect_source, priority_scores
from src.detect.text_filters import (
    drift_adjust,
    emerging_stat_break,
    is_boilerplate,
    specific_mask,
)
from src.ingest import store
from src.ingest.text_terms import PeriodCounter
from src.schemas import DETECTOR_TESTS, SIGNALS, TEXT_DOCS
from tests.test_detect_text import YEARS, null_logits, write_term_freq


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"]["processed_dir"] = str(tmp_path / "processed")
    c["paths"]["signals_dir"] = str(tmp_path / "signals")
    c["fdr"]["calibration"]["n_resamples"] = 499
    c["text_detect"]["workers"] = 1
    c["text_detect"]["min_mean_share"] = 0.005
    return c


def noise_shares(rng, k, g=len(YEARS)):  # noqa: B008
    return {
        f"ruido{i} palabra": 1 / (1 + np.exp(-lg)) for i, lg in enumerate(null_logits(rng, k, g))
    }


def run(cfg, tmp_path, shares, rng):
    write_term_freq(tmp_path / "processed", "edgar", "item_1a", shares, 5, rng)
    return detect_source(cfg, "edgar", "run-rev", datetime.now())


def read_tests(cfg, tmp_path):
    root = tmp_path / "signals"  # los tests viven en processed/lineage/detector_tests
    del root
    return {
        r["object_id"]: r
        for r in store.read_table(
            DETECTOR_TESTS,
            tmp_path / "processed" / "lineage" / "detector_tests",
            source="edgar",
        ).to_pylist()
    }


# --- 1. Punto de quiebre estimado por término ----------------------------------------------------


def test_break_point_is_estimated_per_term(cfg, tmp_path):
    rng = np.random.default_rng(3)
    g = len(YEARS)
    shares = noise_shares(rng, 50)
    late = np.full(g, 0.01)
    late[-2:] = [0.06, 0.12]  # empieza 2 periodos antes del final
    early = np.full(g, 0.01)
    early[-4:] = [0.04, 0.07, 0.10, 0.14]  # empieza 4 periodos antes del final
    shares["tardio nuevo"], shares["temprano nuevo"] = late, early
    run(cfg, tmp_path, shares, rng)
    t = read_tests(cfg, tmp_path)
    assert t["item_1a|tardio nuevo"]["window_start"] == date(YEARS[-2], 1, 1)
    assert t["item_1a|temprano nuevo"]["window_start"] == date(YEARS[-4], 1, 1)
    # No hay un corte fijo: las ventanas de los dos términos difieren.
    assert t["item_1a|tardio nuevo"]["window_start"] != t["item_1a|temprano nuevo"]["window_start"]


def test_break_statistic_maximises_over_segment_length():
    x = np.zeros(14)
    x[-3:] = 4.0
    t, length = emerging_stat_break(x, 7, 2, 4)
    assert float(t) == pytest.approx(4.0) and int(length) == 3


def test_window_never_ends_after_last_document(cfg, tmp_path):
    rng = np.random.default_rng(5)
    g = len(YEARS)
    shares = noise_shares(rng, 50)
    inj = np.full(g, 0.01)
    inj[-3:] = [0.03, 0.06, 0.10]
    shares["inyectado nuevo"] = inj
    last_doc = date(YEARS[-1], 11, 15)  # antes del 31-dic del último año
    docs = pa.Table.from_pylist(
        [
            {
                "source": "edgar",
                "doc_id": "d1",
                "date": last_doc,
                "entity": "1",
                "section": "item_1a",
                "text_hash": "h",
            }
        ],
        schema=TEXT_DOCS.schema,
    )
    store.replace_partition(
        docs, TEXT_DOCS, tmp_path / "processed" / "text_docs", source="edgar", partition="x"
    )
    report = run(cfg, tmp_path, shares, rng)
    assert report["n_bh"] >= 1
    rows = read_tests(cfg, tmp_path).values()
    assert rows and all(r["window_end"] <= last_doc for r in rows)
    sig = store.read_table(SIGNALS, tmp_path / "signals", source="edgar").to_pylist()
    assert sig and all(s["window_end"] <= last_doc for s in sig)
    assert all(r["window_start"] <= r["window_end"] for r in rows)


# --- 2. Deriva global del corpus ----------------------------------------------------------------


def drifting_shares(rng, k=120, bump=0.8):
    """Todos los términos suben juntos (+bump en logit) en los últimos 3 periodos."""
    g = len(YEARS)
    out = {}
    for i, lg in enumerate(null_logits(rng, k, g, rho=0.0)):
        lg = lg.copy()
        lg[-3:] += bump * np.array([0.4, 0.8, 1.0])
        out[f"ruido{i} palabra"] = 1 / (1 + np.exp(-lg))
    return out


def test_global_drift_gives_no_signals_when_controlled(cfg, tmp_path):
    rng = np.random.default_rng(21)
    report = run(cfg, tmp_path, drifting_shares(rng), rng)
    assert report["n_bh"] == 0, report["top"]


def test_global_drift_gives_signals_without_control(cfg, tmp_path):
    """Control de potencia: sin la corrección, la misma deriva sí genera señales falsas."""
    rng = np.random.default_rng(21)
    cfg["text_detect"]["drift"]["bins"] = 0
    report = run(cfg, tmp_path, drifting_shares(rng), rng)
    assert report["n_bh"] > 20


def test_real_term_survives_drift_control(cfg, tmp_path):
    rng = np.random.default_rng(22)
    shares = drifting_shares(rng, bump=0.3)
    inj = np.full(len(YEARS), 0.01)
    inj[-3:] = [0.04, 0.08, 0.14]
    shares["inyectado nuevo"] = inj
    report = run(cfg, tmp_path, shares, rng)
    assert "item_1a|inyectado nuevo" in {o for o, _, _ in report["top"]}


def test_drift_adjust_removes_common_shift_only():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.05, (100, 12))
    x[:, -3:] += 1.0
    x[0, -3:] += 2.0  # un término sí emerge
    adj, _ = drift_adjust(x, slice(0, 9), 5, 10)
    assert abs(adj[1:, -3:].mean()) < 0.1
    assert adj[0, -3:].mean() > 1.5


# --- 3. Especificidad -----------------------------------------------------------------------------


def test_specificity_filters():
    terms = ["send written", "the of", "supply chain", "carbon", "operating", "quickly"]
    mean = np.array([0.01, 0.01, 0.01, 0.01, 0.2, 0.001])
    ok = specific_mask(terms, mean, {"the", "of"}, ["send written"], 0.05)
    assert list(ok) == [False, False, True, True, False, False]
    assert is_boilerplate("please send written comments", ["send written"])
    assert not is_boilerplate("written send", ["send written"])


def test_boilerplate_comes_from_config_not_code(cfg, tmp_path):
    rng = np.random.default_rng(8)
    g = len(YEARS)
    shares = noise_shares(rng, 40)
    inj = np.full(g, 0.01)
    inj[-3:] = [0.03, 0.06, 0.10]
    shares["send written"] = inj
    shares["supply chain"] = inj.copy()
    report = run(cfg, tmp_path, shares, rng)
    found = {o for o, _, _ in report["top"]}
    assert "item_1a|supply chain" in found and "item_1a|send written" not in found
    assert report["prefiltro"]["excluidos_especificidad"] >= 1
    assert "send written" in cfg["text_detect"]["boilerplate"]
    src = (
        open("src/detect/text_filters.py", encoding="utf-8").read()
        + open("src/detect/text.py", encoding="utf-8").read()
    )
    assert "send written" not in src


# --- 4. Amplitud entre industrias y prevalencia base ---------------------------------------


def test_priority_penalises_base_prevalence_and_rewards_industry_breadth():
    keys = [("s", "rare term"), ("s", "common term"), ("s", "wide term"), ("s", "narrow term")]
    effect = np.array([2.0, 2.0, 2.0, 2.0])
    share = np.array([0.001, 0.2, 0.001, 0.001])
    ind = np.array([np.nan, np.nan, 60.0, 2.0])
    cfgp = {"ngram_bonus": 0.25, "base_share_ref": 0.05}
    pr = priority_scores(keys, effect, share, ind, cfgp)
    assert pr[0] > pr[1]  # misma señal: la palabra común no gana
    assert pr[2] > pr[3]  # más industrias, más prioridad


def test_period_counter_counts_distinct_industries():
    c = PeriodCounter()
    h = np.array([1, 2], dtype=np.uint64)
    c.add("s", h, "a", "10")
    c.add("s", h[:1], "b", "10")
    c.add("s", h[:1], "c", "20")
    hashes = np.array([1, 2], dtype=np.uint64)
    assert c.n_groups("s", hashes).tolist() == [2, 1]
    assert c.n_entities("s", hashes).tolist() == [3, 1]
    c2 = PeriodCounter()
    c2.add("s", h, "a")
    assert c2.n_groups("s", hashes) is None  # sin SIC no se inventa la medida
