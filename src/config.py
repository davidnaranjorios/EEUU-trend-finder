"""Carga y validación de config.yaml."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

FREQUENCIES = ("D", "W", "M", "Q", "A")
FDR_METHODS = ("bh", "by")
CALIBRATION_METHODS = ("stationary_bootstrap", "moving_block_bootstrap", "block_permutation")


class ConfigError(ValueError):
    """config.yaml no cumple el contrato esperado."""


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Lee config.yaml, valida su contrato y devuelve el diccionario."""
    with Path(path).open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict[str, Any]) -> None:
    """Lanza ConfigError si falta una sección o un valor está fuera de rango."""
    _section(cfg, "paths")
    _section(cfg, "logging")
    for key in ("min_obs", "max_staleness_days"):
        _check_per_frequency(_section(cfg, "eligibility").get(key), f"eligibility.{key}")
    _check_per_frequency(
        _section(cfg, "persistence").get("min_consecutive_periods"),
        "persistence.min_consecutive_periods",
    )

    fdr = _section(cfg, "fdr")
    q = fdr.get("q")
    if not isinstance(q, int | float) or not 0 < q < 1:
        raise ConfigError(f"fdr.q debe estar en (0, 1); recibido {q!r}")
    if fdr.get("method") != "bh":
        raise ConfigError("fdr.method debe ser 'bh': BY es solo comparación")
    if fdr.get("comparison_method") not in (*FDR_METHODS, None):
        raise ConfigError(f"fdr.comparison_method inválido: {fdr.get('comparison_method')!r}")

    cal = fdr.get("calibration", {})
    if cal.get("method") not in CALIBRATION_METHODS:
        raise ConfigError(
            f"fdr.calibration.method debe ser uno de {CALIBRATION_METHODS}; "
            f"recibido {cal.get('method')!r}"
        )
    block = cal.get("block_length")
    if not (block == "auto" or (isinstance(block, int) and block > 1)):
        raise ConfigError("fdr.calibration.block_length debe ser 'auto' o un entero > 1")
    n = cal.get("n_resamples")
    if not isinstance(n, int) or n < 99:
        raise ConfigError("fdr.calibration.n_resamples debe ser un entero >= 99")

    _validate_detect(_section(cfg, "detect"))
    _validate_relative(_section(cfg, "detect"), _section(cfg, "eligibility").get("young") or {})
    _validate_fred(_section(cfg, "fred"))
    _validate_eligibility(_section(cfg, "eligibility"))
    _validate_vintages(_section(cfg, "vintages"))
    _validate_groups(_section(cfg, "download").get("groups"), "download.groups")
    v2_limit = cfg["download"].get("v2_page_limit")
    if not isinstance(v2_limit, int) or not 0 < v2_limit <= 500000:
        raise ConfigError("download.v2_page_limit debe estar en 1..500000")
    _validate_sources(_section(cfg, "sources"))
    _validate_exports(_section(cfg, "exports"))
    _validate_text(cfg)
    _validate_rank(_section(cfg, "rank"))
    _validate_context(_section(cfg, "context"))
    storage = _section(cfg, "storage")
    for key in ("bucket", "prefix"):
        if not isinstance(storage.get(key), str) or not storage[key]:
            raise ConfigError(f"storage.{key} debe ser un texto no vacío")


def in_young_scope(group_name: str | None, geo_level: str | None, cfg: dict[str, Any]) -> bool:
    """Una serie del grupo joven (p. ej. historia corta) con nivel geográfico descargable.

    Solo amplía la DESCARGA y el detector `relative_growth` (ADR 0017); `download.groups` sigue
    siendo la lista que ven slope, regime, el explorador y los exportes."""
    young = (cfg.get("eligibility") or {}).get("young") or {}
    return group_name in (young.get("groups") or []) and geo_level in cfg["download"]["groups"]


# Grupos del embudo que se pueden descargar (los demás son exclusiones o diagnósticos).
DOWNLOADABLE_GROUPS = ("nacional", "regional", "internacional", "nuevas")
SEASONAL_CODES = ("SA", "SAAR", "SSA", "NSA", "NSAAR")


def _validate_eligibility(el: dict[str, Any]) -> None:
    years = el.get("min_history_years")
    if not isinstance(years, int | float) or years <= 0:
        raise ConfigError("eligibility.min_history_years debe ser positivo")
    variants = el.get("variants")
    if not isinstance(variants, dict):
        raise ConfigError("falta eligibility.variants")
    sa = variants.get("seasonal_preference")
    if not isinstance(sa, list) or sorted(sa) != sorted(SEASONAL_CODES):
        raise ConfigError(f"eligibility.variants.seasonal_preference debe ordenar {SEASONAL_CODES}")
    freq = variants.get("frequency_preference")
    if not isinstance(freq, list) or sorted(freq) != sorted(FREQUENCIES):
        raise ConfigError(f"eligibility.variants.frequency_preference debe ordenar {FREQUENCIES}")
    regex = variants.get("transformed_units_regex")
    try:
        re.compile(regex)
    except (TypeError, re.error) as exc:
        raise ConfigError(f"eligibility.variants.transformed_units_regex inválida: {exc}") from exc


DETECTORS = ("slope", "regime")
GRIDS = ("M", "Q", "A")


def _validate_relative(d: dict[str, Any], young: dict[str, Any]) -> None:
    """`detect.relative_growth` y `eligibility.young` (series jóvenes, ADR 0017)."""
    rg = d.get("relative_growth") or {}
    if not isinstance(rg.get("enabled"), bool):
        raise ConfigError("detect.relative_growth.enabled debe ser true o false")
    if rg.get("confidence") not in ("yellow", "green", "red"):
        raise ConfigError("detect.relative_growth.confidence debe ser yellow, green o red")
    if not isinstance(rg.get("phi_margin"), int | float) or rg["phi_margin"] < 0:
        raise ConfigError("detect.relative_growth.phi_margin debe ser un número >= 0")
    mp = rg.get("min_periods") or {}
    if any(not isinstance(mp.get(g), int) or mp[g] < 8 for g in GRIDS):
        raise ConfigError("detect.relative_growth.min_periods: un entero >= 8 por grilla M, Q y A")
    pp = rg.get("parent") or {}
    for key in ("min_common_tokens", "max_tree_levels"):
        if not isinstance(pp.get(key), int) or pp[key] < 1:
            raise ConfigError(f"detect.relative_growth.parent.{key} debe ser un entero >= 1")
    b = rg.get("births") or {}
    if not isinstance(b.get("enabled"), bool):
        raise ConfigError("detect.relative_growth.births.enabled debe ser true o false")
    for key in ("naics_digits", "recent_years", "min_years", "min_total"):
        if not isinstance(b.get(key), int) or b[key] < 1:
            raise ConfigError(f"detect.relative_growth.births.{key} debe ser un entero >= 1")
    if not isinstance(b.get("phi_margin"), int | float) or b["phi_margin"] < 0:
        raise ConfigError("detect.relative_growth.births.phi_margin debe ser un número >= 0")
    if not 2 <= b["naics_digits"] <= 6:
        raise ConfigError("detect.relative_growth.births.naics_digits debe estar entre 2 y 6")
    groups = young.get("groups")
    if not isinstance(groups, list) or not groups or not all(isinstance(g, str) for g in groups):
        raise ConfigError("eligibility.young.groups debe ser una lista no vacía de grupos")
    lo, hi = young.get("min_years"), young.get("max_years")
    if not isinstance(lo, int) or not isinstance(hi, int) or not 1 <= lo < hi:
        raise ConfigError("eligibility.young: min_years y max_years enteros con 1 <= min < max")


def _validate_detect(d: dict[str, Any]) -> None:
    sources = d.get("sources")
    if (
        not isinstance(sources, list)
        or not sources
        or not all(isinstance(x, str) and x for x in sources)
        or len(set(sources)) != len(sources)
    ):
        raise ConfigError("detect.sources debe ser una lista no vacía de fuentes distintas")
    dets = d.get("detectors")
    if not isinstance(dets, list) or not dets or not set(dets) <= set(DETECTORS):
        raise ConfigError(f"detect.detectors debe ser una lista no vacía de {DETECTORS}")
    win = d.get("slope_window")
    if not isinstance(win, dict) or set(win) != set(GRIDS):
        raise ConfigError(f"detect.slope_window debe definir las grillas {GRIDS}")
    if any(not isinstance(v, int) or v < 3 for v in win.values()):
        raise ConfigError("detect.slope_window: enteros >= 3")
    for key in ("lookback_years", "workers", "slope_min_baseline"):
        if not isinstance(d.get(key), int) or d[key] < 1:
            raise ConfigError(f"detect.{key} debe ser un entero positivo")
    if not isinstance(d.get("regime_escalate"), int) or d["regime_escalate"] < 0:
        raise ConfigError("detect.regime_escalate debe ser un entero >= 0")
    if not isinstance(d.get("phi_margin"), int | float) or d["phi_margin"] < 0:
        raise ConfigError("detect.phi_margin debe ser >= 0")
    if not isinstance(d.get("winsor_mad"), int | float) or d["winsor_mad"] <= 0:
        raise ConfigError("detect.winsor_mad debe ser positivo")
    share = d.get("max_gap_share")
    if not isinstance(share, int | float) or not 0 <= share < 1:
        raise ConfigError("detect.max_gap_share debe estar en [0, 1)")
    tail = d.get("tail")
    if not isinstance(tail, dict) or tail.get("method") not in ("gpd", "none"):
        raise ConfigError("detect.tail.method debe ser 'gpd' o 'none'")
    pelt = d.get("pelt")
    if not isinstance(pelt, dict) or not isinstance(pelt.get("jump"), int) or pelt["jump"] < 1:
        raise ConfigError("detect.pelt.jump debe ser un entero >= 1")


def _validate_vintages(v: dict[str, Any]) -> None:
    for key in ("sample_size", "seed"):
        if not isinstance(v.get(key), int) or v[key] < 0:
            raise ConfigError(f"vintages.{key} debe ser un entero no negativo")
    cap = v.get("max_vintage_dates_per_request")
    if not isinstance(cap, int) or not 0 < cap <= 2000:
        raise ConfigError("vintages.max_vintage_dates_per_request debe estar en 1..2000")
    _validate_groups(v.get("groups"), "vintages.groups")


def _validate_exports(ex: dict[str, Any]) -> None:
    _validate_groups(ex.get("groups"), "exports.groups")
    n = ex.get("n_buckets")
    if not isinstance(n, int) or not 0 < n <= 100:
        raise ConfigError("exports.n_buckets debe ser un entero en 1..100")
    for key in ("max_file_kb", "max_total_mb"):
        if not isinstance(ex.get(key), int | float) or ex[key] <= 0:
            raise ConfigError(f"exports.{key} debe ser positivo")


def _validate_groups(groups: Any, name: str) -> None:
    if not isinstance(groups, list) or not groups or not set(groups) <= set(DOWNLOADABLE_GROUPS):
        raise ConfigError(
            f"{name} debe ser una lista no vacía con valores de {DOWNLOADABLE_GROUPS}"
        )


# Fuentes con adaptador sobre src/ingest/common.py (Fase 4).
COMMON_SOURCES = ("bls", "bea", "census")


def _validate_sources(src: dict[str, Any]) -> None:
    if not isinstance(src.get("n_buckets"), int) or not 0 < src["n_buckets"] <= 100:
        raise ConfigError("sources.n_buckets debe ser un entero en 1..100")
    if not isinstance(src.get("user_agent_env"), str) or not src["user_agent_env"]:
        raise ConfigError("sources.user_agent_env debe nombrar una variable de entorno")
    for name in COMMON_SOURCES:
        s = src.get(name)
        if not isinstance(s, dict):
            raise ConfigError(f"falta la sección 'sources.{name}'")
        rpm = s.get("requests_per_minute")
        if not isinstance(rpm, int | float) or not 0 < rpm <= 100:
            raise ConfigError(f"sources.{name}.requests_per_minute debe estar en (0, 100]")
        for key in ("max_retries", "catalog_refresh_days", "write_batch_rows"):
            if not isinstance(s.get(key), int) or s[key] < 1:
                raise ConfigError(f"sources.{name}.{key} debe ser un entero positivo")
        for key in ("backoff_base_s", "timeout_s"):
            if not isinstance(s.get(key), int | float) or s[key] <= 0:
                raise ConfigError(f"sources.{name}.{key} debe ser positivo")
        share = s.get("crawl_budget_share", 0.5)
        if not isinstance(share, int | float) or not 0 < share <= 1:
            raise ConfigError(f"sources.{name}.crawl_budget_share debe estar en (0, 1]")
        for key in ("unit_max_min", "hard_grace_min"):
            v = s.get(key)
            if v is not None and (not isinstance(v, int | float) or v <= 0):
                raise ConfigError(f"sources.{name}.{key} debe ser null o positivo (minutos)")
        for key in ("deadline_check_rows", "progress_rows"):
            v = s.get(key)
            if v is not None and (not isinstance(v, int) or v < 1):
                raise ConfigError(f"sources.{name}.{key} debe ser un entero positivo")
        agg = s.get("lineage_aggregate_min_series")
        if agg is not None and (not isinstance(agg, int) or agg < 2):
            raise ConfigError(
                f"sources.{name}.lineage_aggregate_min_series debe ser null o un entero >= 2"
            )


def _section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(cfg.get(name), dict):
        raise ConfigError(f"falta la sección '{name}'")
    return cfg[name]


def _validate_fred(fred: dict[str, Any]) -> None:
    rpm = fred.get("requests_per_minute")
    if not isinstance(rpm, int | float) or not 0 < rpm <= 120:
        raise ConfigError("fred.requests_per_minute debe estar en (0, 120] (límite oficial)")
    if not isinstance(fred.get("n_buckets"), int) or not 0 < fred["n_buckets"] <= 100:
        raise ConfigError("fred.n_buckets debe ser un entero en 1..100")
    if not 0 < fred.get("page_limit_series", 0) <= 1000:
        raise ConfigError("fred.page_limit_series debe estar en 1..1000")
    if not 0 < fred.get("page_limit_observations", 0) <= 100000:
        raise ConfigError("fred.page_limit_observations debe estar en 1..100000")
    if not 0 < fred.get("updates_fallback_days", 0) < 14:
        raise ConfigError("fred.updates_fallback_days debe ser < 14 (ventana de series/updates)")
    for key in ("max_retries", "obs_batch_size", "catalog_refresh_days", "verification_seed"):
        if not isinstance(fred.get(key), int) or fred[key] < 0:
            raise ConfigError(f"fred.{key} debe ser un entero no negativo")
    for key in ("backoff_base_s", "timeout_s"):
        if not isinstance(fred.get(key), int | float) or fred[key] <= 0:
            raise ConfigError(f"fred.{key} debe ser positivo")


def _validate_text_detect(td: dict[str, Any]) -> None:
    grid = td.get("grid")
    if not isinstance(grid, dict) or not grid or not set(grid.values()) <= set(GRIDS):
        raise ConfigError(f"text_detect.grid: una grilla de {GRIDS} por fuente")
    for key in ("window", "recent"):
        win = td.get(key)
        if not isinstance(win, dict) or set(win) != set(GRIDS):
            raise ConfigError(f"text_detect.{key} debe definir las grillas {GRIDS}")
        if any(not isinstance(v, int) or v < 1 for v in win.values()):
            raise ConfigError(f"text_detect.{key}: enteros positivos")
    for key in ("min_mean_share", "max_median_share"):
        v = td.get(key)
        if not isinstance(v, int | float) or not 0 < v < 1:
            raise ConfigError(f"text_detect.{key} debe estar en (0, 1)")
    ents = td.get("min_entities")
    if not isinstance(ents, dict) or any(not isinstance(v, int) or v < 0 for v in ents.values()):
        raise ConfigError("text_detect.min_entities: entero >= 0 por fuente (0 = sin filtro)")
    if not isinstance(td.get("phi_margin"), int | float) or td["phi_margin"] < 0:
        raise ConfigError("text_detect.phi_margin debe ser >= 0")
    for key in ("block_sample", "workers"):
        if not isinstance(td.get(key), int) or td[key] < 1:
            raise ConfigError(f"text_detect.{key} debe ser un entero positivo")


def _validate_text(cfg: dict[str, Any]) -> None:
    _validate_text_detect(_section(cfg, "text_detect"))
    text = _section(cfg, "text")
    if text.get("ngram_max") not in (1, 2, 3):
        raise ConfigError("text.ngram_max debe ser 1, 2 o 3")
    for key in ("min_doc_count", "seed", "top_sic"):
        if not isinstance(text.get(key), int) or text[key] < 1:
            raise ConfigError(f"text.{key} debe ser un entero positivo")
    if (
        not isinstance(text.get("open_refresh_hours"), int | float)
        or text["open_refresh_hours"] <= 0
    ):
        raise ConfigError("text.open_refresh_hours debe ser positivo")
    if not isinstance(text.get("workers"), int) or text["workers"] < 0:
        raise ConfigError("text.workers debe ser un entero >= 0 (0 = sin procesos)")
    edgar = _section(cfg, "edgar")
    rps = edgar.get("requests_per_second")
    if not isinstance(rps, int | float) or not 0 < rps <= 10:
        raise ConfigError("edgar.requests_per_second debe estar en (0, 10] (límite oficial)")
    if not isinstance(edgar.get("forms"), list) or not edgar["forms"]:
        raise ConfigError("edgar.forms debe ser una lista no vacía")
    for name in ("edgar", "federal_register"):
        sec = _section(cfg, name)
        for key in ("start_year", "min_chars", "closed_grace_days", "max_retries"):
            if not isinstance(sec.get(key), int) or sec[key] < 0:
                raise ConfigError(f"{name}.{key} debe ser un entero no negativo")
        if (
            not isinstance(sec.get("requests_per_second"), int | float)
            or sec["requests_per_second"] <= 0
        ):
            raise ConfigError(f"{name}.requests_per_second debe ser positivo")


def _check_per_frequency(value: Any, name: str) -> None:
    if not isinstance(value, dict) or set(value) != set(FREQUENCIES):
        raise ConfigError(f"{name} debe definir exactamente las frecuencias {FREQUENCIES}")
    for freq, n in value.items():
        if not isinstance(n, int) or n < 1:
            raise ConfigError(f"{name}.{freq} debe ser un entero positivo")


EMBEDDING_BACKENDS = ("fastembed", "hashing")


def _validate_rank(r: dict[str, Any]) -> None:
    """Principio 4: nunca menos de dos fuentes independientes; pesos completos y no negativos."""
    ms = r.get("min_sources")
    if not isinstance(ms, int) or ms < 2:
        raise ConfigError("rank.min_sources debe ser un entero >= 2 (triangulación)")
    if not isinstance(r.get("max_candidates"), int) or r["max_candidates"] < 1:
        raise ConfigError("rank.max_candidates debe ser un entero positivo")
    w = r.get("weights") or {}
    # Que estén todos los componentes lo verifica src.rank.triangulate.score.
    if not w or any(not isinstance(v, int | float) or v < 0 for v in w.values()):
        raise ConfigError("rank.weights: un peso >= 0 por componente del score")
    if sum(w.values()) <= 0:
        raise ConfigError("rank.weights: al menos un peso positivo")
    if (r.get("embeddings") or {}).get("backend") not in EMBEDDING_BACKENDS:
        raise ConfigError(f"rank.embeddings.backend debe ser uno de {EMBEDDING_BACKENDS}")
    o = r.get("origin") or {}
    if not isinstance(o.get("table"), str) or not o["table"]:
        raise ConfigError("rank.origin.table debe ser la ruta de la tabla data_origin")
    for key in ("duplicate_rho", "level_tol"):
        if not isinstance(o.get(key), int | float) or not 0 <= o[key] <= 1:
            raise ConfigError(f"rank.origin.{key} debe estar en [0, 1]")
    if not isinstance(o.get("min_overlap"), int) or o["min_overlap"] < 2:
        raise ConfigError("rank.origin.min_overlap debe ser un entero >= 2")
    c = r.get("cluster") or {}
    for key in ("alpha", "threshold", "duplicate_rho", "duplicate_cos"):
        if not isinstance(c.get(key), int | float) or not 0 <= c[key] <= 1:
            raise ConfigError(f"rank.cluster.{key} debe estar en [0, 1]")
    re_ = r.get("rate_effect") or {}
    if not isinstance(re_.get("max_lag"), int) or re_["max_lag"] < 0:
        raise ConfigError("rank.rate_effect.max_lag debe ser un entero >= 0")
    if not isinstance(re_.get("min_drop"), int | float) or not 0 < re_["min_drop"] <= 1:
        raise ConfigError("rank.rate_effect.min_drop debe estar en (0, 1]")
    if not isinstance(re_.get("include_level"), bool):
        raise ConfigError("rank.rate_effect.include_level debe ser true o false")
    mr = re_.get("min_rows_by_grid") or {}
    if any(not isinstance(mr.get(g), int) or mr[g] < 8 for g in ("M", "Q", "A")):
        raise ConfigError("rank.rate_effect.min_rows_by_grid: un entero >= 8 por grilla M, Q y A")
    ie = r.get("inflation_effect") or {}
    if not isinstance(ie.get("max_lag"), int) or ie["max_lag"] < 0:
        raise ConfigError("rank.inflation_effect.max_lag debe ser un entero >= 0")
    if not isinstance(ie.get("min_drop"), int | float) or not 0 < ie["min_drop"] <= 1:
        raise ConfigError("rank.inflation_effect.min_drop debe estar en (0, 1]")
    if not isinstance(ie.get("seed"), int):
        raise ConfigError("rank.inflation_effect.seed debe ser un entero")
    imr = ie.get("min_rows_by_grid") or {}
    if any(not isinstance(imr.get(g), int) or imr[g] < 8 for g in ("M", "Q", "A")):
        raise ConfigError(
            "rank.inflation_effect.min_rows_by_grid: un entero >= 8 por grilla M, Q y A"
        )
    wl = r.get("single_origin") or {}
    if not isinstance(wl.get("min_effect"), int | float) or wl["min_effect"] <= 0:
        raise ConfigError("rank.single_origin.min_effect debe ser un número > 0")
    cm = (r.get("links") or {}).get("comove") or {}
    tg = cm.get("text_grid") or {}
    if not isinstance(tg, dict) or any(g not in ("Q", "A") for g in tg.values()):
        raise ConfigError("rank.links.comove.text_grid: una grilla Q o A por fuente de texto")
    mo = cm.get("min_overlap_by_grid") or {}
    for grid in {"A", *tg.values()}:
        if not isinstance(mo.get(grid), int) or mo[grid] < 4:
            raise ConfigError(
                f"rank.links.comove.min_overlap_by_grid.{grid} debe ser un entero >= 4"
            )
    lk = r.get("links") or {}
    q = lk.get("null_quantile")
    if not isinstance(q, int | float) or not 0 < q < 1:
        raise ConfigError("rank.links.null_quantile debe estar en (0, 1)")


REFERENCE_KEYS = ("deflator", "population", "recessions", "short_rate")


def _validate_context(c: dict[str, Any]) -> None:
    """Ficha del fenómeno: exactamente cuatro series de referencia metodológica (ADR 0009, 0013)."""
    refs = c.get("reference_series")
    if not isinstance(refs, dict) or set(refs) != set(REFERENCE_KEYS):
        raise ConfigError(f"context.reference_series debe tener exactamente {REFERENCE_KEYS}")
    if not all(isinstance(v, str) and v for v in refs.values()):
        raise ConfigError("context.reference_series: un id de serie por clave")
    for key in ("roles", "min_overlap"):
        if not isinstance(c.get(key), dict) or not c[key]:
            raise ConfigError(f"context.{key} debe ser un mapeo no vacío")
    for pattern in [*c["roles"].values(), c.get("nominal_units"), c.get("not_nominal_units")]:
        try:
            re.compile(pattern)
        except (re.error, TypeError) as exc:
            raise ConfigError(f"context: regex inválida {pattern!r}") from exc
