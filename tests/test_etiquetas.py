"""Etiquetas (ADR 0035): dirección «desacelera» y clase política/contable por `release_id`."""

from __future__ import annotations

import pytest

from src.rank import measure
from src.rank.phenomena import display_direction
from src.report.render import _policy_excluded


def test_frena_subida_is_shown_as_desacelera_not_baja():
    assert display_direction("baja", "frena_subida") == "desacelera"


@pytest.mark.parametrize(
    "direction,traj",
    [
        ("baja", "caida_sobre_valle"),
        ("baja", None),
        ("sube", "frena_subida"),
        ("mixto", "frena_subida"),
    ],
)
def test_baja_is_kept_for_levels_that_fall(direction, traj):
    assert display_direction(direction, traj) == direction


def test_h41_release_is_policy_class_by_release_id_not_by_series_id():
    rules = measure.load_rules()
    # Cualquier id de serie, incluso inventado: lo decide el release_id de FRED (H.4.1 = 20).
    for sid, units in (("WLAD", "Millions of Dollars"), ("XYZ", "Percent"), ("QQQ", "Index")):
        m = measure.classify(rules, source="fred", series_id=sid, units=units, release_ids=[20])
        assert m.kind == measure.POLICY_KIND and m.rule_id == "politica_contable_h41"
    other = measure.classify(rules, source="fred", series_id="WLAD", units="Millions of Dollars")
    assert other.kind != measure.POLICY_KIND  # sin el release no se clasifica como política


def test_release_id_must_be_a_positive_integer():
    bad = {
        "version": 1,
        "rules": [{"id": "x", "kind": "politica_contable", "match": {"release_id": "20"}}],
    }
    with pytest.raises(ValueError, match="release_id"):
        measure.validate_table(bad)


def test_policy_section_shows_the_count():
    html = _policy_excluded({"params_json": '{"summary": {"n_policy_excluded": 7}}'})
    assert "Política o contable (excluidas)" in html and "<strong>7</strong>" in html
    assert _policy_excluded({}) == ""
