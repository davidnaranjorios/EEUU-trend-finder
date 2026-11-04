"""Revisión 2a02c981: n_industries (SIC) en la ingesta y ejemplos de empresas (entrega D)."""

from __future__ import annotations

import copy

import pytest

from src.config import load_config
from src.detect.text_examples import find_examples, recent_filings, to_markdown
from src.ingest.text import run_source
from tests.fake_text import FakeEdgar
from tests.test_text_ingest import TODAY, edgar, read_all


@pytest.fixture
def cfg():
    c = copy.deepcopy(load_config())
    c["text"]["min_doc_count"] = 2
    c["text"]["workers"] = 0
    c["edgar"]["start_year"] = 2024
    c["edgar"]["min_chars"] = 200
    return c


def ingest(cfg, tmp_path, fake):
    processed = tmp_path / "processed"
    src = edgar(cfg, fake, tmp_path)
    run_source(src, cfg, processed_dir=processed, run_id="r1", today=TODAY, only_periods=["2024Q1"])
    return src, processed


def test_ingest_stores_distinct_industries_from_sic(cfg, tmp_path):
    fake = FakeEdgar(inject="zeta omega kappa", inject_docs=2)
    _, processed = ingest(cfg, tmp_path, fake)
    freq = {(r["section"], r["term"]): r for r in read_all(processed, "edgar")["term_freq"]}
    inj = freq[("item_1a", "zeta omega kappa")]
    # CIK 101 (SIC 28xx) y CIK 102 (SIC 73xx): dos industrias, dos empresas.
    assert (inj["n_entities"], inj["n_industries"]) == (2, 2)
    assert all(r["n_industries"] <= r["doc_count"] for r in freq.values())


def test_examples_give_company_name_and_cik_of_recent_docs(cfg, tmp_path):
    fake = FakeEdgar(inject="zeta omega kappa", inject_docs=2)
    src, processed = ingest(cfg, tmp_path, fake)
    period, filings = recent_filings(processed, "edgar")
    assert period == "2024Q1" and filings
    wanted = ["item_1a|zeta omega kappa", "item_1a|no existe aqui"]
    ex = find_examples(src, filings, wanted, per_term=3, sample=50)
    got = ex["item_1a|zeta omega kappa"]
    assert {e["cik"] for e in got} == {"101", "102"}  # solo los documentos que lo contienen
    assert {e["company"] for e in got} == {"Company 101 Inc", "Company 102 Inc"}
    assert ex["item_1a|no existe aqui"] == []
    top = [
        {
            "object_id": o,
            "effect_size": 2.0,
            "priority": 1.5,
            "q_value": 0.001,
            "n_industries": 2.0,
            "window_start": "2023-01-01",
            "window_end": "2024-03-04",
        }
        for o in wanted
    ]
    md = to_markdown(top, ex, period)
    assert "Company 101 Inc (101)" in md and "sin ejemplo en la muestra" in md
