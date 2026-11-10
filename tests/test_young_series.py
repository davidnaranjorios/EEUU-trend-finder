"""Series jóvenes (ADR 0017): detector `relative_growth`, padre desde metadatos y nacimientos."""

from __future__ import annotations

import copy
import csv
import json
from collections import Counter
from datetime import UTC, date, datetime

import numpy as np
import pyarrow as pa
import pytest

from src.catalog import connect
from src.config import REPO_ROOT, ConfigError, in_young_scope, load_config, validate_config
from src.detect import births as bm
from src.detect.parents import Meta, find_parent, is_ancestor, own_code
from src.detect.relative import Pair, difference, relative_stat, run_family
from src.detect.run import (
    DETECTOR_TESTS,
    finalize,
    load_series,
    load_young_series,
    run_detectors,
    table_root,
)
from src.detect.young import rel_params
from src.ingest.store import upsert
from src.rank import naics as naics_mod
from src.schemas import ELIGIBILITY, SERIES, SERIES_META
from src.stats.bootstrap import ar_null  # noqa: F401  (el nulo vive en relative.py)
from src.stats.fdr import bh_adjust

CFG0 = load_config()
TABLES = naics_mod.load_tables(CFG0, REPO_ROOT)


def _names() -> dict[str, str]:
    with (REPO_ROOT / CFG0["rank"]["naics"]["names_table"]).open(encoding="utf-8") as fh:
        return {r["naics"]: r["name"] for r in csv.DictReader(fh)}


def _pairs_of_codes(n: int) -> list[tuple[str, str]]:
    """(código de 3 dígitos, código hijo de 4) con nombres oficiales únicos y distintos."""
    names = _names()
    freq = Counter(names.values())
    out = []
    for code in sorted(names):
        if len(code) == 4 and code[:3] in names and code[:2] in names:
            if freq[names[code]] == 1 and freq[names[code[:3]]] == 1:
                if names[code] != names[code[:3]]:
                    out.append((code[:3], code))
    seen, picked = set(), []
    for parent, child in out:
        if parent not in seen and len(picked) < n:
            seen.add(parent)
            picked.append((parent, child))
    return picked


# --- padre desde metadatos -------------------------------------------------------------------


def _meta(sid, title, *, units="Thousands", sa="SA", freq="M", path="Survey A;x"):
    return Meta("bls", sid, title, path, units, sa, freq)


def _uni(rows):
    """rows: (sid, título, ...kw) -> universo {clave: (meta, código propio)}."""
    meta = {}
    for sid, title, kw in rows:
        meta[("bls", sid)] = _meta(sid, title, **kw)
    links = naics_mod.series_links(
        {k: f"{m.title} | {m.category_path}" for k, m in meta.items()}, TABLES
    )
    return {k: (m, own_code(links.get(k))) for k, m in meta.items()}


def test_nearest_naics_ancestor_is_the_parent_and_ranges_expand():
    names = _names()
    (p3, c4) = _pairs_of_codes(1)[0]
    uni = _uni(
        [
            ("child", f"Employment Level, {names[c4]}", {}),
            ("parent3", f"Employment Level, {names[p3]}", {}),
            ("grand2", f"Employment Level, {names[c4[:2]]}", {}),
        ]
    )
    parent, why = find_parent(uni[("bls", "child")][0], uni, TABLES)
    assert why is None and parent.key == ("bls", "parent3") and parent.how == "naics_prefijo"
    assert parent.code == p3
    assert (
        is_ancestor("31-33", "3361")
        and not is_ancestor("3361", "336")
        and not is_ancestor("54", "51")
    )


def test_survey_total_is_the_fallback_only_when_unique():
    names = _names()
    (_, c4) = _pairs_of_codes(1)[0]
    base = [("child", f"Employment Level, {names[c4]}", {})]
    uni = _uni([*base, ("total", "Employment Level", {})])
    parent, _ = find_parent(uni[("bls", "child")][0], uni, TABLES)
    assert parent.key == ("bls", "total") and parent.how == "total_encuesta" and parent.code is None
    uni2 = _uni([*base, ("t1", "Employment Level", {}), ("t2", "Employment Level", {})])
    parent, why = find_parent(uni2[("bls", "child")][0], uni2, TABLES)
    assert parent is None and why == "padre_ambiguo"
    uni3 = _uni(base)
    assert find_parent(uni3[("bls", "child")][0], uni3, TABLES) == (None, "sin_padre")


def test_parent_must_share_survey_measure_units_adjustment_and_frequency():
    names = _names()
    (p3, c4) = _pairs_of_codes(1)[0]
    child = ("child", f"Employment Level, {names[c4]}", {})
    for kw in (
        {"units": "Percent"},
        {"sa": "NSA"},
        {"freq": "Q"},
        {"path": "Survey B;x"},
    ):
        uni = _uni([child, ("parent3", f"Employment Level, {names[p3]}", kw)])
        assert find_parent(uni[("bls", "child")][0], uni, TABLES)[0] is None, kw
    other = _uni([child, ("parent3", f"Hours Worked, {names[p3]}", {})])  # otra medida
    assert find_parent(other[("bls", "child")][0], other, TABLES)[0] is None
    nocode = _uni([("c", "Employment Level", {}), ("t", "Employment Level", {})])
    assert find_parent(nocode[("bls", "c")][0], nocode, TABLES) == (None, "sin_naics")


# --- estadístico y FDR sobre ruido ---------------------------------------------------------


def _ar1(rng, n, phi):
    e = rng.normal(0, 1, n + 50)
    x = np.zeros(n + 50)
    for t in range(1, n + 50):
        x[t] = phi * x[t - 1] + e[t]
    return x[50:]


def test_isolated_jump_gives_no_signal_and_a_sustained_gap_does():
    params = rel_params(CFG0)
    rng = np.random.default_rng(1)
    base = [Pair(rng.normal(0, 0.001, 48), 100, 3, "M") for _ in range(30)]
    jump = rng.normal(0, 0.001, 48)
    jump[20] += 0.05  # un salto aislado: empuja la media pero no se sostiene
    sustained = rng.normal(0, 0.001, 48) + 0.04
    res, _ = run_family([*base, Pair(jump, 100, 3, "M"), Pair(sustained, 100, 3, "M")], params)
    q = bh_adjust(np.array([r.p_value for r in res]))
    assert q[-2] > 0.05 and res[-2].p_value > 0.05  # el salto aislado no genera señal
    assert q[-1] <= 0.05 and res[-1].persistent and res[-1].effect > 0  # la brecha sostenida sí
    # La persistencia está dentro del estadístico: una racha corta da T = 0 y p = 1.
    t_short, *_ = relative_stat(np.r_[np.zeros(20), [0.05], np.zeros(27)], 3)
    assert float(t_short) == 0.0


def test_difference_aligns_the_common_period():
    gc, gp = np.arange(40.0), np.arange(100.0)
    d, last = difference(gc, 500, gp, 500, 36)
    assert last == 500 and d.size == 40 and np.allclose(d, gc - gp[-40:])
    d, last = difference(gc, 500, gp, 520, 36)  # el padre termina 20 periodos después
    assert last == 500 and d.size == 40 and np.allclose(d, gc - gp[-60:-20])
    assert difference(gc, 500, gp[:30], 500, 36) is None  # el padre solo cubre 30 < 36


@pytest.mark.parametrize("phi", [0.3, 0.7, 0.9])
def test_false_discoveries_stay_controlled_on_pure_noise_with_ar_and_correlation(phi):
    """Ruido AR(1) y correlación entre series (factor común): el FDR de BH queda dentro de q."""
    params = rel_params(CFG0)
    reps, any_rejection, total = 5, 0, 0
    rng = np.random.default_rng(int(phi * 100))
    for _ in range(reps):
        common = _ar1(rng, 36, phi)
        pairs = [
            Pair(0.005 * (0.7 * common + 0.7 * _ar1(rng, 36, phi)), 1000, 3, "M") for _ in range(80)
        ]
        res, _ = run_family(pairs, params)
        q = bh_adjust(np.array([r.p_value if r else 1.0 for r in res]))
        any_rejection += bool((q <= 0.05).any())
        total += int((q <= 0.05).sum())
    # Bajo el nulo completo FDR = P(al menos un rechazo); con 5 familias se tolera una.
    assert any_rejection <= 1, f"φ={phi}: {total} rechazos BH en {reps} familias de ruido"


def test_family_shares_blocks_per_grid_and_length_and_shards_match():
    params = rel_params(CFG0)
    rng = np.random.default_rng(5)
    pairs = [Pair(0.005 * _ar1(rng, 36, 0.3), 100, 3, "M") for _ in range(6)]
    pairs.append(Pair(0.005 * _ar1(rng, 40, 0.3), 100, 3, "M"))
    full, blocks = run_family(pairs, params)
    assert set(blocks) == {("M", 36), ("M", 40)}
    sel = [i % 2 == 0 for i in range(len(pairs))]
    part, blocks2 = run_family(pairs, params, sel)
    assert blocks == blocks2
    for i, (a, b) in enumerate(zip(full, part, strict=True)):
        assert (b is None) == (not sel[i])
        if b is not None:
            assert a.p_value == b.p_value


# --- elegibilidad: las series jóvenes solo llegan a relative_growth ------------------------


def _month(i: int, start_year: int = 2000) -> date:
    return date(start_year + i // 12, i % 12 + 1, 1)


def _write(cfg, specs):
    """specs: dict sid -> (título, growth[], group, geo, start_year); source bls, SA mensual."""
    processed = table_root(cfg, "detector_tests").parent.parent
    rows, elig, meta = [], [], []
    for sid, (title, g, group, geo, y0) in specs.items():
        level = 100 * np.exp(np.cumsum(g))
        rows += [
            {
                "source": "bls",
                "series_id": sid,
                "date": _month(i, y0),
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
                "run_id": f"e-{sid}",
                "source": "bls",
                "series_id": sid,
                "group_name": group,
                "rule": "clasificada",
                "detail": None,
                "geo_level": geo,
                "frequency": "M",
                "copyrighted": False,
                "decided_at": datetime(2026, 11, 1, tzinfo=UTC),
            }
        )
        meta.append(
            {
                "source": "bls",
                "series_id": sid,
                "title": title,
                "category_path": "Survey A;sector",
                "start_date": _month(0, y0),
                "end_date": _month(len(g) - 1, y0),
                "last_updated": None,
                "discontinued": False,
                "copyrighted": False,
            }
        )
    for _table, spec, data, sub in (
        ("series", SERIES, rows, "series"),
        ("elig", ELIGIBILITY, elig, "lineage/eligibility"),
        ("meta", SERIES_META, meta, "series_meta"),
    ):
        upsert(
            pa.Table.from_pylist(data, schema=spec.schema),
            spec,
            processed / sub,
            source="bls",
            n_buckets=4,
        )
    return processed


N_FAM = 14


def _specs(rng):
    """Padres (3 dígitos, 20 años) y jóvenes (4 dígitos, 4 años): 2 al triple, el resto iguales."""
    names = _names()
    specs, kinds = {}, {}
    for i, (p3, c4) in enumerate(_pairs_of_codes(N_FAM)):
        gp = 0.003 + 0.0005 * rng.standard_normal(240)
        specs[f"par{i:02d}"] = (f"Employment Level, {names[p3]}", gp, "nacional", "nacional", 2000)
        triple = i < 2
        gc = (3 * gp[-48:] if triple else gp[-48:]) + 0.0005 * rng.standard_normal(48)
        specs[f"kid{i:02d}"] = (f"Employment Level, {names[c4]}", gc, "nuevas", "nacional", 2016)
        kinds[f"kid{i:02d}"] = "triple" if triple else "igual"
    return specs, kinds


@pytest.fixture(scope="module")
def young_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("young")
    cfg = copy.deepcopy(CFG0)
    cfg["paths"]["processed_dir"] = str(tmp / "data" / "processed")
    cfg["paths"]["signals_dir"] = str(tmp / "data" / "signals")
    cfg["logging"]["runs_file"] = str(tmp / "data" / "logs" / "runs.jsonl")
    cfg["detect"]["workers"] = 1
    cfg["detect"]["relative_growth"]["births"]["enabled"] = False
    rng = np.random.default_rng(11)
    specs, kinds = _specs(rng)
    # Casos que NO deben entrar al detector joven:
    names = _names()
    (p3, c4) = _pairs_of_codes(N_FAM + 1)[-1]
    specs["old"] = (
        f"Employment Level, {names[c4]}",
        0.003 + 0.0005 * rng.standard_normal(240),
        "nuevas",
        "nacional",
        2000,
    )
    specs["regional"] = (
        f"Employment Level, {names[c4]} Region",
        0.003 + 0.0005 * rng.standard_normal(48),
        "nuevas",
        "regional",
        2016,
    )
    specs["twelve"] = (
        f"Employment Level, {names[p3]}",
        0.003 + 0.0005 * rng.standard_normal(144),
        "nacional",
        "nacional",
        2008,
    )
    _write(cfg, specs)
    started = datetime.now(UTC)
    stats = run_detectors(cfg, "runY")
    report = finalize(cfg, "runY", started)
    return cfg, kinds, stats, report


def test_young_series_reach_only_the_relative_growth_detector(young_run):
    cfg, kinds, stats, _ = young_run
    con = connect(cfg["paths"]["processed_dir"])
    # slope y regime (load_series) nunca ven las jóvenes; load_young_series sí, y solo a ellas.
    normal = {s.series_id for s in load_series(con, cfg)}
    young = {s.series_id for s in load_young_series(con, cfg)}
    assert young == set(kinds) and not (young & normal)
    by_det = dict(
        con.execute(
            "SELECT detector, list(DISTINCT object_id) FROM detector_tests GROUP BY 1"
        ).fetchall()
    )
    assert set(by_det) == {"slope", "regime", "relative_growth"}
    assert set(by_det["relative_growth"]) <= set(kinds)
    assert not (set(by_det["slope"]) | set(by_det["regime"])) & set(kinds)
    assert stats["relative_growth"]["young_loaded"] == len(kinds)


def test_older_than_the_window_regional_and_non_young_groups_do_not_enter(young_run):
    cfg, kinds, _, _ = young_run
    con = connect(cfg["paths"]["processed_dir"])
    young = {s.series_id for s in load_young_series(con, cfg)}
    assert "old" not in young  # 20 años > max_years: va a slope y regime (grupo y datos)
    assert "regional" not in young  # nivel geográfico no descargable
    assert "twelve" not in young  # grupo nacional, no joven
    assert "old" not in {
        r[0]
        for r in con.execute(
            "SELECT object_id FROM detector_tests WHERE detector = 'relative_growth'"
        ).fetchall()
    }


def test_triple_growth_is_detected_and_equal_growth_is_not(young_run):
    cfg, kinds, stats, report = young_run
    con = connect(cfg["paths"]["processed_dir"])
    sig = {
        o: (e, q)
        for o, e, q in con.execute(
            "SELECT object_id, effect_size, q_value FROM signals WHERE detector = 'relative_growth'"
        ).fetchall()
    }
    triples = {k for k, v in kinds.items() if v == "triple"}
    assert triples <= set(sig), (stats["relative_growth"], sig)
    assert all(sig[t][0] > 0 and sig[t][1] <= 0.05 for t in triples)
    assert not (set(sig) & {k for k, v in kinds.items() if v == "igual"})


def test_own_fdr_family_with_by_comparison_and_default_confidence(young_run):
    cfg, kinds, stats, report = young_run
    rg = stats["relative_growth"]
    assert rg["confidence"] == "yellow" and rg["with_parent"] == len(kinds) == rg["tested"]
    assert rg["parent_by_method"] == {"naics_prefijo": len(kinds)}
    d = report["detectors"]["relative_growth"]
    assert d["n_tests"] == len(kinds) and d["n_by"] <= d["n_bh"]
    con = connect(cfg["paths"]["processed_dir"])
    runs = dict(con.execute("SELECT detector, n_tests FROM detector_runs").fetchall())
    assert runs["relative_growth"] == len(kinds)
    assert runs["slope"] != runs["relative_growth"]  # familias separadas, cada una con su tamaño


def test_slope_and_regime_calibration_is_unchanged_by_the_young_detector(young_run):
    cfg, kinds, _, report = young_run
    normal = {f"par{i:02d}" for i in range(N_FAM)} | {"old", "twelve"}
    assert report["detectors"]["slope"]["n_tests"] <= len(normal)
    assert report["detectors"]["regime"]["n_tests"] <= len(normal)


# --- nacimiento de series -------------------------------------------------------------------


P_BIRTH = bm.BirthParams(3, 3, 8, 20, 499, "auto", 7, 3.0, rel_params(CFG0).tail, 0.05)


def _counts(rng, n_groups, years=15, rate=6.0, surge=None):
    out = {}
    for g in range(n_groups):
        c = rng.poisson(rate, years)
        if surge is not None and g in surge:
            c[-3:] += 25
        out[("bls", f"{300 + g}")] = {2000 + i: int(v) for i, v in enumerate(c)}
    return out


def test_unusual_series_creation_is_flagged_and_stable_creation_is_not():
    rng = np.random.default_rng(2)
    counts = _counts(rng, 40, surge={0, 1})
    rows = bm.run_births(counts, 2014, P_BIRTH)
    flagged = {r["naics"] for r in rows if r["flagged"]}
    assert {"300", "301"} <= flagged and len(flagged) <= 3
    top = rows[0]
    assert top["direction"] == "mas" and top["recent_per_year"] > top["prior_per_year"]
    assert all(r["n_tests"] == len(rows) for r in rows)


def test_births_false_discoveries_are_controlled_on_stable_creation():
    flagged = 0
    for seed in range(5):
        rows = bm.run_births(_counts(np.random.default_rng(seed), 60), 2014, P_BIRTH)
        assert len(rows) == 60
        flagged += sum(r["flagged"] for r in rows)
    assert flagged <= 2  # 300 pruebas de ruido con q = 0,05


def test_births_skip_short_or_small_groups_and_group_codes_by_digits():
    counts = {
        ("bls", "300"): {2013: 30, 2014: 30},
        ("bls", "301"): {y: 1 for y in range(2000, 2015)},
    }
    assert bm.run_births(counts, 2014, P_BIRTH) == []  # historia corta / pocas series
    ys = bm.yearly_counts(
        [("bls", "5132", date(2010, 3, 1)), ("bls", None, date(2010, 3, 1)), ("bls", "5132", None)]
        and [
            ("bls", "5132", date(2010, 3, 1)),
            ("bls", None, date(2011, 1, 1)),
            ("bls", "31-33", date(2012, 1, 1)),
        ],
        3,
        2014,
    )
    assert ys == {("bls", "513"): {2010: 1}, ("bls", "31-33"): {2012: 1}}


def test_births_are_descriptive_and_never_enter_signals(tmp_path):
    cfg = copy.deepcopy(CFG0)
    cfg["paths"]["processed_dir"] = str(tmp_path / "data" / "processed")
    cfg["paths"]["signals_dir"] = str(tmp_path / "data" / "signals")
    cfg["logging"]["runs_file"] = str(tmp_path / "data" / "logs" / "runs.jsonl")
    cfg["detect"]["workers"] = 1
    rng = np.random.default_rng(4)
    specs, _ = _specs(rng)
    _write(cfg, {k: v for k, v in specs.items() if k.startswith("par")})
    started = datetime.now(UTC)
    run_detectors(cfg, "runB")
    report = finalize(cfg, "runB", started)
    b = report["births"]
    assert b["descriptive"] is True and "error" not in b
    assert b["n_tested"] == 0  # un solo año de nacimiento por grupo: sin historia que comparar
    con = connect(cfg["paths"]["processed_dir"])
    dets = {r[0] for r in con.execute("SELECT DISTINCT detector FROM signals").fetchall()}
    assert dets <= {"slope", "regime"}


# --- configuración y alcance de la descarga ------------------------------------------------


def test_config_validates_young_and_relative_growth_options():
    cfg = json.loads(json.dumps(CFG0))
    validate_config(cfg)
    for path, value in (
        (("detect", "relative_growth", "confidence"), "blue"),
        (("detect", "relative_growth", "phi_margin"), -1),
        (("detect", "relative_growth", "min_periods", "M"), 3),
        (("detect", "relative_growth", "births", "naics_digits"), 9),
        (("eligibility", "young", "groups"), []),
        (("eligibility", "young", "max_years"), 2),
    ):
        bad = json.loads(json.dumps(CFG0))
        node = bad
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        with pytest.raises(ConfigError):
            validate_config(bad)


def test_young_scope_widens_only_downloads_never_download_groups():
    cfg = json.loads(json.dumps(CFG0))
    assert in_young_scope("nuevas", "nacional", cfg)
    assert not in_young_scope("nuevas", "regional", cfg)  # geografía no descargable
    assert not in_young_scope("nacional", "nacional", cfg)  # ese ya está en download.groups
    assert "nuevas" not in cfg["download"]["groups"]  # slope, regime, explorador y exportes igual


def test_detector_tests_schema_accepts_the_new_detector_name():
    assert DETECTOR_TESTS.schema.field("detector").type == pa.string()


def test_lineage_records_the_chosen_parent_for_every_young_series(young_run):
    from src.detect.young import lineage_path

    cfg, kinds, _, report = young_run
    lin = json.loads(lineage_path(cfg, "runY").read_text("utf-8"))
    assert set(lin) == {f"bls|{k}" for k in kinds}
    for key, row in lin.items():
        sid = key.split("|")[1]
        assert row["parent"] == f"bls|par{sid[3:]}" and row["how"] == "naics_prefijo"
        assert row["parent_naics"] == row["child_naics"][:3] and row["periods"] == 47
    rg = report["relative_growth"]
    assert rg["confidence"] == "yellow" and rg["n_young"] == len(kinds)
    assert (
        rg["parent_by_method"] == {"naics_prefijo": len(kinds)} and not rg["not_tested_by_reason"]
    )
