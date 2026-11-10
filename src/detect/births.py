"""«Nacimiento de series»: cuántas series nuevas aparecen por agencia y NAICS (ADR 0017).

DESCRIPTIVO: no es una señal, no entra al ranking ni al score. Para cada (agencia, NAICS de 3–4
dígitos) cuenta las series cuya `start_date` cae en cada año calendario y pregunta si la creación
de los últimos `recent_years` es inusual frente a la historia del grupo (más series nuevas, o
menos). Una agencia que empieza a publicar muchos códigos de una industria es una pista de que
algo cambió ahí (o de que cambió su catálogo: la lectura es humana).

Estadístico: |t| de la media de los últimos R años frente a la de los previos (conteo anual).
Nulo: stationary bootstrap de la línea base (años previos, centrada) con post-blackening AR(1) y
los MISMOS índices de bloques para todos los grupos del mismo largo; p con la cola GPD; BH sobre
todos los grupos probados (una familia por corrida, q = fdr.q).
Límite: solo se ven las series presentes en `series_meta`.
La agencia es la fuente del adaptador (BLS, BEA, Census); FRED agrega a otras agencias.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from src.stats.bootstrap import (
    AR_BURN_IN,
    ar_null,
    family_block_length,
    fit_ar1,
    stationary_indices,
)
from src.stats.fdr import TailConfig, bh_adjust, bootstrap_p

Group = tuple[str, str]  # (agencia, NAICS)


@dataclass(frozen=True)
class BirthParams:
    digits: int
    recent_years: int
    min_years: int
    min_total: int
    n_resamples: int
    block_length: int | str
    seed: int
    phi_margin: float
    tail: TailConfig
    q: float


def group_code(code: str | None, digits: int) -> str | None:
    """NAICS del grupo: los primeros `digits` dígitos (un rango "31-33" se deja como está)."""
    if not code:
        return None
    return code if "-" in code else code[:digits]


def yearly_counts(
    starts: Iterable[tuple[str, str | None, date | None]], digits: int, last_year: int
) -> dict[Group, dict[int, int]]:
    """Series que empiezan en cada año, por (agencia, NAICS); `starts`: (agencia, código, fecha)."""
    acc: dict[Group, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for agency, code, start in starts:
        g = group_code(code, digits)
        if g is None or start is None or start.year > last_year:
            continue
        acc[(agency, g)][start.year] += 1
    return {k: dict(v) for k, v in acc.items()}


def birth_stat(c: np.ndarray, recent: int) -> np.ndarray:
    """|t| del conteo anual `c` (..., n): media de los últimos `recent` años frente a la de los
    previos, dividida por su error estándar (sd de los años previos, al menos 1). Estudentizar
    hace el estadístico casi pivote y el nulo calibra mejor con pocos años."""
    base, last = c[..., :-recent], c[..., -recent:]
    sd = np.maximum(base.std(axis=-1, ddof=1), 1.0)
    se = sd * np.sqrt(1.0 / recent + 1.0 / base.shape[-1])
    return np.abs(last.mean(axis=-1) - base.mean(axis=-1)) / se


def run_births(
    counts: Mapping[Group, Mapping[int, int]], last_year: int, p: BirthParams
) -> list[dict[str, Any]]:
    """Prueba todos los grupos con historia y conteo suficientes; devuelve filas con su q."""
    series: dict[Group, np.ndarray] = {}
    years: dict[Group, int] = {}
    for g, by_year in counts.items():
        first = min(by_year)
        n = last_year - first + 1
        if n < p.min_years or sum(by_year.values()) < p.min_total or n <= p.recent_years + 1:
            continue
        series[g] = np.array([by_year.get(y, 0) for y in range(first, last_year + 1)], float)
        years[g] = first
    if not series:
        return []
    by_len: dict[int, list[Group]] = defaultdict(list)
    for g, c in series.items():
        by_len[c.size].append(g)
    rows: list[dict[str, Any]] = []
    for n, groups in sorted(by_len.items()):
        block = family_block_length([series[g] for g in groups], p.block_length)
        # Como en `slope`, el nulo sale de la línea base (los años previos a la ventana reciente):
        # si saliera de toda la serie, el propio aumento inflaría la varianza del nulo.
        idx = stationary_indices(
            n - p.recent_years,
            p.n_resamples,
            block,
            (p.seed, n, p.recent_years),
            n_out=n + AR_BURN_IN,
        )
        for g in sorted(groups):
            c = series[g]
            t_obs = float(birth_stat(c, p.recent_years))
            base = c[: -p.recent_years]
            base = base - base.mean()
            null = birth_stat(ar_null(base, idx, fit_ar1(base, p.phi_margin)), p.recent_years)
            pv = 1.0 if t_obs == 0.0 else float(bootstrap_p(t_obs, null, p.tail)[0])
            recent, prior = float(c[-p.recent_years :].mean()), float(c[: -p.recent_years].mean())
            rows.append(
                {
                    "agency": g[0],
                    "naics": g[1],
                    "first_year": years[g],
                    "n_years": n,
                    "n_series": int(c.sum()),
                    "recent_per_year": recent,
                    "prior_per_year": prior,
                    "direction": "mas" if recent > prior else "menos",
                    "p_value": pv,
                }
            )
    q = bh_adjust(np.array([r["p_value"] for r in rows]))
    for r, qv in zip(rows, q, strict=True):
        r["q_value"], r["n_tests"] = float(qv), len(rows)
        r["flagged"] = bool(qv <= p.q)
    return sorted(rows, key=lambda r: (r["q_value"], r["agency"], r["naics"]))


def compute(cfg: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Cuenta y prueba los nacimientos de series de todas las fuentes de `detect.sources`."""
    from src.catalog import connect
    from src.config import REPO_ROOT
    from src.detect.parents import own_code
    from src.rank import naics as naics_mod

    b = cfg["detect"]["relative_growth"]["births"]
    cal = cfg["fdr"]["calibration"]
    params = BirthParams(
        digits=b["naics_digits"],
        recent_years=b["recent_years"],
        min_years=b["min_years"],
        min_total=b["min_total"],
        n_resamples=cal["n_resamples"],
        block_length=cal["block_length"],
        seed=cal["seed"],
        phi_margin=float(b["phi_margin"]),
        tail=TailConfig(**cfg["detect"]["tail"]),
        q=float(cfg["fdr"]["q"]),
    )
    last_year = (today or date.today()).year - 1  # el año en curso está incompleto
    con = connect(REPO_ROOT / cfg["paths"]["processed_dir"])
    rows = con.execute(
        "SELECT source, series_id, any_value(title), any_value(category_path), "
        "any_value(start_date) FROM series_meta WHERE list_contains(?, source) GROUP BY 1, 2",
        [list(cfg["detect"]["sources"])],
    ).fetchall()
    con.close()
    tables = naics_mod.load_tables(cfg, REPO_ROOT)
    links = naics_mod.series_links(
        {(s, i): f"{t or ''} | {c or ''}" for s, i, t, c, _ in rows}, tables
    )
    counts = yearly_counts(
        ((s, own_code(links.get((s, i))), st) for s, i, _t, _c, st in rows),
        params.digits,
        last_year,
    )
    tested = run_births(counts, last_year, params)
    return {
        "descriptive": True,
        "last_year": last_year,
        "recent_years": params.recent_years,
        "n_groups_counted": len(counts),
        "n_tested": len(tested),
        "n_flagged": sum(r["flagged"] for r in tested),
        "groups": tested,
    }
