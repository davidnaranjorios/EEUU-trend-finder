"""Carga y validación de config.yaml."""

from __future__ import annotations

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

    _validate_fred(_section(cfg, "fred"))


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


def _check_per_frequency(value: Any, name: str) -> None:
    if not isinstance(value, dict) or set(value) != set(FREQUENCIES):
        raise ConfigError(f"{name} debe definir exactamente las frecuencias {FREQUENCIES}")
    for freq, n in value.items():
        if not isinstance(n, int) or n < 1:
            raise ConfigError(f"{name}.{freq} debe ser un entero positivo")
