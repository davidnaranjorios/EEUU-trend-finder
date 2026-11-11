"""Padre por categoría/título (ADR 0020): FRED sin NAICS, y BLS/BEA/Census sin código."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime

import numpy as np
import pyarrow as pa
import pytest

from src.catalog import connect
from src.config import REPO_ROOT, ConfigError, load_config, validate_config
from src.detect.parents import (
    Meta,
    ParentParams,
    category_chain,
    common_prefix,
    find_parent,
    title_tokens,
)
from src.detect.run import finalize, load_young_series, run_detectors, table_root
from src.detect.young import lineage_path
from src.ingest.store import upsert
from src.rank import naics as naics_mod
from src.schemas import ELIGIBILITY, SERIES, SERIES_META

CFG0 = load_config()
TABLES = naics_mod.load_tables(CFG0, REPO_ROOT)
D0, D1 = date(2000, 1, 1), date(2018, 1, 1)


def meta(sid, title, path, *, src="fred", units="Thousands", sa="SA", freq="M", start=D0):
    return Meta(src, sid, title, path, units, sa, freq, start)


def uni(*ms):
    return {m.key: (m, None) for m in ms}


def parent_of(child, *others, params=None):
    return find_parent(child, uni(child, *others), TABLES, params)


CAT = "Business > Employment"
SUB = "Business > Employment > Industry"


# --- métodos puros ----------------------------------------------------------------------------


def test_title_tokens_drop_function_words_and_punctuation():
    assert title_tokens("All Employees: Total, in the U.S.") == (
        "all",
        "employees",
        "total",
        "u",
        "s",
    )
    assert common_prefix(("a", "b", "c"), ("a", "b", "d")) == 2


def test_category_chain_goes_up_the_fred_tree_and_stays_in_the_unit_elsewhere():
    m = meta("X", "t", "A > B > C;A > D")
    chain = category_chain(m, ParentParams(2, 3))
    assert (0, "A > B > C") in chain and (1, "A > B") in chain
    # "A" cuelga de las dos ramas: gana el nivel más cercano (1, por "A > D").
    assert (0, "A > D") in chain and (1, "A") in chain and (2, "A") not in chain
    assert [
        c for lv, c in category_chain(m, ParentParams(2, 1)) if lv == 2
    ] == []  # tope de niveles
    bls = meta("Y", "t", "Survey A;sector", src="bls")
    assert category_chain(bls, ParentParams(2, 3)) == [(0, "Survey A")]


def test_ancestor_category_aggregate_is_the_parent_of_a_fred_series_without_naics():
    child = meta("KID", "All Employees: Software Publishers", SUB, start=D1)
    parent = meta("TOT", "All Employees", CAT)
    got, why = parent_of(child, parent)
    assert why is None and got.key == ("fred", "TOT")
    assert got.how == "titulo_agregado" and got.level == 1


def test_same_category_needs_containment_and_beats_an_ancestor():
    child = meta("KID", "All Employees: Software Publishers", SUB, start=D1)
    contained = meta("MID", "All Employees: Software", SUB)  # el título del padre es prefijo
    ancestor = meta("TOT", "All Employees", CAT)
    got, _ = parent_of(child, contained, ancestor)
    assert got.key == ("fred", "MID") and got.level == 0  # el nivel más cercano gana
    # Un hermano que no está contenido (otra rama del título) no es padre en la misma categoría.
    sibling = meta("SIB", "All Employees: Manufacturing", SUB)
    got, _ = parent_of(child, sibling, ancestor)
    assert got.key == ("fred", "TOT") and got.level == 1


def test_candidate_must_share_measure_be_older_and_not_a_duplicate_title():
    child = meta("KID", "All Employees: Software Publishers", SUB, start=D1)
    for bad in (
        meta("P", "All Employees", CAT, units="Percent"),
        meta("P", "All Employees", CAT, sa="NSA"),
        meta("P", "All Employees", CAT, freq="Q"),
        meta("P", "All Employees", CAT, start=date(2020, 1, 1)),  # más joven que la hija
        meta("P", "All Employees: Software Publishers", SUB),  # mismo título: duplicado
        meta("P", "Employees", CAT),  # prefijo común de una sola palabra (< 2)
    ):
        assert parent_of(child, bad)[0] is None, bad
    assert parent_of(child, meta("P", "All Employees", CAT, src="bls"))[0] is None  # otra fuente


def test_tree_levels_are_limited_and_the_nearest_level_wins():
    far = "Business"
    child = meta(
        "KID",
        "All Employees: Software Publishers",
        "Business > Employment > Industry > X",
        start=D1,
    )
    top = meta("TOP", "All Employees", far)
    assert parent_of(child, top, params=ParentParams(2, 3))[0].level == 3
    assert parent_of(child, top, params=ParentParams(2, 2))[0] is None  # no llega tan arriba
    mid = meta("MID", "All Employees", "Business > Employment")
    assert parent_of(child, top, mid, params=ParentParams(2, 3))[0].key == ("fred", "MID")


def test_ambiguity_between_equally_specific_different_titles_is_not_resolved():
    child = meta("KID", "All Employees: Software Publishers", SUB, start=D1)
    a = meta("A", "All Employees", CAT)
    b = meta("B", "All Workers", CAT)  # no comparte prefijo: no compite
    assert parent_of(child, a, b)[0].key == ("fred", "A")
    c = meta("C", "All Employees Total", CAT)  # más específico (3 palabras): pierde frente a A
    assert parent_of(child, a, c)[0].key == ("fred", "A")
    # Dos títulos distintos con el mismo prefijo y la misma longitud: empate real.
    child2 = meta("KID2", "Hourly Earnings Production Workers Total", SUB, start=D1)
    x = meta("X", "Hourly Earnings Production", CAT)
    y = meta("Y", "Hourly Earnings Production", CAT)  # mismo título: no es empate (duplicado)
    assert parent_of(child2, x, y)[0].key == ("fred", "X")
    z = meta("Z", "Hourly Earnings Nonsupervisory", CAT)  # prefijo común más corto: pierde
    assert parent_of(child2, x, z)[0].key == ("fred", "X")


def test_without_naics_other_sources_use_their_unit_and_never_look_up_the_tree():
    child = meta("C", "Population Level - Women", "Survey A;sector", src="bls", start=D1)
    parent = meta("P", "Population Level", "Survey A;other", src="bls")
    got, why = parent_of(child, parent)
    assert why is None and got.how == "titulo_agregado" and got.level == 0
    other_unit = meta("P2", "Population Level", "Survey B;x", src="bls")
    assert parent_of(child, other_unit)[0] is None  # otra encuesta: no es su agregado
    contained = meta("P3", "Population Level - Women Over", "Survey A;x", src="bls")
    assert parent_of(child, contained)[0] is None  # más específico: no es agregado (misma unidad)


def test_naics_methods_keep_priority_over_the_title():
    names = {r: n for r, n in TABLES.names.items()}
    c4 = next(c for c in sorted(names) if len(c) == 4 and c[:3] in names)
    child = Meta(
        "bls", "KID", f"Employment Level, {names[c4]}", "Survey A;x", "Thousands", "SA", "M", D1
    )
    by_naics = Meta(
        "bls", "P3", f"Employment Level, {names[c4[:3]]}", "Survey A;x", "Thousands", "SA", "M", D0
    )
    by_title = Meta("bls", "TT", "Employment Level", "Survey A;x", "Thousands", "SA", "M", D0)
    links = naics_mod.series_links(
        {m.key: f"{m.title} | {m.category_path}" for m in (child, by_naics, by_title)}, TABLES
    )
    from src.detect.parents import own_code

    universe = {m.key: (m, own_code(links.get(m.key))) for m in (child, by_naics, by_title)}
    got, _ = find_parent(child, universe, TABLES)
    assert got.how == "naics_prefijo" and got.key == ("bls", "P3")


# --- corrida completa: FRED sin NAICS --------------------------------------------------------


def _m(i, y0=2016):
    return date(y0 + i // 12, i % 12 + 1, 1)


def _write_fred(cfg, specs):
    """specs: sid -> (título, categoría, crecimiento, grupo, año_inicio). Fuente fred, una corrida
    del embudo (FRED usa la última corrida entera)."""
    processed = table_root(cfg, "detector_tests").parent.parent
    rows, elig, meta_rows = [], [], []
    for sid, (title, path, g, group, y0) in specs.items():
        level = 100 * np.exp(np.cumsum(g))
        rows += [
            {
                "source": "fred",
                "series_id": sid,
                "date": _m(i, y0),
                "value": float(v),
                "vintage_date": None,
                "frequency": "M",
                "units": "Thousands",
                "seasonal_adjustment": "SA",
            }
            for i, v in enumerate(level)
        ]
        elig.append(
            {
                "run_id": "e1",
                "source": "fred",
                "series_id": sid,
                "group_name": group,
                "rule": "clasificada",
                "detail": None,
                "geo_level": "nacional",
                "frequency": "M",
                "copyrighted": False,
                "decided_at": datetime(2026, 11, 1, tzinfo=UTC),
            }
        )
        meta_rows.append(
            {
                "source": "fred",
                "series_id": sid,
                "title": title,
                "category_path": path,
                "start_date": _m(0, y0),
                "end_date": _m(len(g) - 1, y0),
                "last_updated": None,
                "discontinued": False,
                "copyrighted": False,
            }
        )
    for spec, data, sub in (
        (SERIES, rows, "series"),
        (ELIGIBILITY, elig, "lineage/eligibility"),
        (SERIES_META, meta_rows, "series_meta"),
    ):
        upsert(
            pa.Table.from_pylist(data, schema=spec.schema), spec, processed / sub,
            source="fred", n_buckets=4,
        )  # fmt: skip


@pytest.fixture(scope="module")
def fred_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fredpadre")
    cfg = copy.deepcopy(CFG0)
    cfg["paths"]["processed_dir"] = str(tmp / "data" / "processed")
    cfg["paths"]["signals_dir"] = str(tmp / "data" / "signals")
    cfg["logging"]["runs_file"] = str(tmp / "data" / "logs" / "runs.jsonl")
    cfg["detect"]["workers"] = 1
    cfg["detect"]["relative_growth"]["births"]["enabled"] = False
    rng = np.random.default_rng(21)
    gp = 0.003 + 0.0001 * rng.standard_normal(240)  # agregado suave: ver ADR 0017 (potencia)
    specs = {"TOTAL": ("All Employees", CAT, gp, "nacional", 2000)}
    kinds = {}
    for i in range(14):
        triple = i < 2
        g = (3 * gp[-48:] if triple else gp[-48:]) + 0.0001 * rng.standard_normal(48)
        sid = f"KID{i:02d}"
        specs[sid] = (f"All Employees: Sector {i}", SUB, g, "nuevas", 2016)
        kinds[sid] = "triple" if triple else "igual"
    _write_fred(cfg, specs)
    started = datetime.now(UTC)
    stats = run_detectors(cfg, "runF")
    report = finalize(cfg, "runF", started)
    return cfg, kinds, stats, report


def test_fred_young_series_without_naics_now_get_the_category_aggregate(fred_run):
    cfg, kinds, stats, report = fred_run
    con = connect(cfg["paths"]["processed_dir"])
    assert {s.series_id for s in load_young_series(con, cfg)} == set(kinds)
    rg = stats["relative_growth"]
    assert rg["with_parent"] == len(kinds) == rg["tested"]
    assert rg["parent_by_method"] == {"titulo_agregado": len(kinds)}
    assert not rg["not_tested_by_reason"]  # antes: todas `sin_naics`
    lin = json.loads(lineage_path(cfg, "runF").read_text("utf-8"))
    assert all(v["parent"] == "fred|TOTAL" and v["tree_level"] == 1 for v in lin.values())
    assert all(v["how"] == "titulo_agregado" and v["child_naics"] is None for v in lin.values())
    assert report["relative_growth"]["parent_by_method"] == {"titulo_agregado": len(kinds)}


def test_same_calibration_triple_is_detected_and_equal_growth_is_not(fred_run):
    cfg, kinds, _, report = fred_run
    con = connect(cfg["paths"]["processed_dir"])
    sig = {
        o: q
        for o, q in con.execute(
            "SELECT object_id, q_value FROM signals WHERE detector = 'relative_growth'"
        ).fetchall()
    }
    assert {k for k, v in kinds.items() if v == "triple"} <= set(sig)
    assert not set(sig) & {k for k, v in kinds.items() if v == "igual"}
    assert report["detectors"]["relative_growth"]["n_tests"] == len(kinds)  # familia propia


def test_calibration_keys_are_untouched_by_this_change():
    rg = CFG0["detect"]["relative_growth"]
    assert rg["phi_margin"] == 5.0 and rg["min_periods"] == {"M": 36, "Q": 24, "A": 12}
    assert CFG0["fdr"]["q"] == 0.05


def test_config_validates_the_parent_options():
    cfg = json.loads(json.dumps(CFG0))
    validate_config(cfg)
    for key in ("min_common_tokens", "max_tree_levels"):
        bad = json.loads(json.dumps(CFG0))
        bad["detect"]["relative_growth"]["parent"][key] = 0
        with pytest.raises(ConfigError):
            validate_config(bad)
