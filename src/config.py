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
    storage = _section(cfg, "storage")
    for key in ("bucket", "prefix"):
        if not isinstance(storage.get(key), str) or not storage[key]:
            raise ConfigError(f"storage.{key} debe ser un texto no vacío")


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


def _validate_detect(d: dict[str, Any]) -> None:
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


def _validate_text(cfg: dict[str, Any]) -> None:
    text = _section(cfg, "text")
    if text.get("ngram_max") not in (1, 2, 3):
        raise ConfigError("text.ngram_max debe ser 1, 2 o 3")
    for key in ("min_doc_count", "seed"):
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
