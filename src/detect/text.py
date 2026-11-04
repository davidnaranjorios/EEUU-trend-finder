"""Términos emergentes: crecimiento sostenido de la proporción de documentos (Fase 3, parte B).

Uso: `uv run python -m src.detect.text --source edgar | federal_register`

Una familia FDR por fuente y corrida (CLAUDE.md, "Reglas estadísticas"); la unidad de prueba es
el par (sección, término) de `term_freq`. Todo sale de los datos: no hay listas de palabras.

1. **Grilla.** `term_freq` se agrega a la grilla del detector (`text_detect.grid`): EDGAR anual
   por año de presentación (los trimestres mezclan cohortes de cierre fiscal), Federal Register
   trimestral. Solo entran periodos completos (todos sus subperiodos con documentos).
2. **Censura.** Un n-grama sin fila en un subperiodo tuvo menos de `text.min_doc_count`
   documentos; se imputa en el tope (`min_doc_count - 1`), lo que nunca infla un pasado bajo:
   es conservador para "crecimiento".
3. **Prefiltro sin mirar el estadístico** (no sesga la FDR): palabras funcionales detectadas en
   los datos (unigramas con proporción mediana >= `max_median_share`; se excluyen ellos y los
   n-gramas que empiezan o terminan en ellos) y prevalencia media >= `min_mean_share`.
4. **Estadístico.** x_t = logit((c+0.5)/(n+1)) (Jeffreys). T = min de x_t en los últimos `k`
   periodos (persistencia de config) menos la media de los `W` periodos previos a la ventana
   reciente `R` (R >= k): solo es grande si todos los últimos k periodos están elevados, así
   que un salto aislado no cuenta.
4b. **Quiebre y deriva** (revisión 2a02c981). El inicio del crecimiento de cada término se estima:
   T es el máximo sobre el largo de segmento L en [persistencia, `recent`] (`emerging_stat_break`),
   y la ventana del término empieza en el periodo `G - L`; el máximo también está en el nulo.
   Antes, cada término se corrige por la deriva del corpus (mediana de cambio de los términos de
   prevalencia base parecida, `drift_adjust`); estadístico y nulo usan la serie corregida.
   La ventana nunca termina después del último documento (`text_docs`).
5. **Nulo.** Post-blackening AR(1) de la historia previa a la ventana reciente R (si el
   crecimiento empezó dentro de R no contamina el nulo; medido: con el inicio en la línea base,
   φ subía de 0,2 a 0,6 y el p de un término inyectado pasaba de 1e-10 a 4e-4)
   (`src.stats.bootstrap.ar_null`, ADR 0007), con **los mismos índices** de stationary bootstrap
   para toda la familia; p con cola GPD (`src.stats.fdr.bootstrap_p`). BH decide, BY compara.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import random
import sys
import zlib
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np

from src.config import REPO_ROOT, load_config
from src.detect.approval import alert_flags, latest_runs
from src.detect.run import signal_id, table_root, write
from src.detect.text_filters import drift_adjust, emerging_stat_break, specific_mask
from src.log import get_logger, run
from src.schemas import DETECTOR_RUNS, DETECTOR_TESTS, SIGNALS
from src.stats.bootstrap import (
    AR_BURN_IN,
    ar_null,
    family_block_length,
    fit_ar1,
    stationary_indices,
)
from src.stats.fdr import TailConfig, bh_adjust, bootstrap_p, by_adjust

DETECTOR = "text_emerging"
SUBPERIODS = {("Q", "A"): 4, ("M", "A"): 12, ("M", "Q"): 3, ("M", "M"): 1, ("Q", "Q"): 1}


# --- Grilla ------------------------------------------------------------------------------------


def sub_frequency(period: str) -> str:
    """'2024Q1' -> 'Q'; '2024-01' -> 'M'."""
    return "Q" if "Q" in period else "M"


def to_grid(period: str, grid: str) -> str:
    """Periodo de `term_freq` -> periodo de la grilla ('2024Q3' -> '2024' en A)."""
    year = period[:4]
    if grid == "A":
        return year
    if grid == "Q":
        if "Q" in period:
            return period
        return f"{year}Q{(int(period[5:7]) - 1) // 3 + 1}"
    return period


def grid_start(gp: str) -> date:
    if len(gp) == 4:
        return date(int(gp), 1, 1)
    if "Q" in gp:
        return date(int(gp[:4]), 3 * (int(gp[-1]) - 1) + 1, 1)
    return date(int(gp[:4]), int(gp[5:7]), 1)


def grid_end(gp: str) -> date:
    if len(gp) == 4:
        return date(int(gp), 12, 31)
    if "Q" in gp:
        q = int(gp[-1])
        return date(int(gp[:4]), 3 * q, 31 if q in (1, 4) else 30)
    y, m = int(gp[:4]), int(gp[5:7])
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


# --- Carga y prefiltro -------------------------------------------------------------------------


@dataclass
class TermMatrix:
    """Conteos (K, G) en la grilla, con totales (S, G) por sección y la clave de cada fila."""

    periods: list[str]
    keys: list[tuple[str, str]]  # (section, term)
    counts: np.ndarray  # (K, G) documentos con el término (censura imputada)
    totals: np.ndarray  # (K, G) documentos de la sección
    excluded: dict[str, int]
    mean_share: np.ndarray = field(default_factory=lambda: np.zeros(0))  # (K,) prevalencia base
    industries: np.ndarray = field(default_factory=lambda: np.zeros(0))  # (K,) NaN = no medido
    last_doc_date: date | None = None


def load_matrix(
    files: Sequence[Path],
    grid: str,
    min_doc_count: int,
    min_mean_share: float,
    max_median_share: float,
    min_entities: int = 0,
    recent: int = 0,
    boilerplate: Sequence[str] = (),
    unigram_max_share: float = 1.0,
    min_industries: int = 0,
    docs_files: Sequence[Path] = (),
) -> TermMatrix:
    """Lee `term_freq` de una fuente, aplica el prefiltro y arma la matriz en la grilla.

    Con `min_entities` > 0 se excluyen los términos cuya amplitud (mediana de `n_entities` en
    los subperiodos medidos de los últimos `recent` periodos de la grilla) es menor: un término
    que solo usa una agencia (p. ej. el nombre de quien firma sus avisos) no es un fenómeno
    amplio. Los términos sin medida (NULL, particiones anteriores al esquema 2) no se excluyen.
    """
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    ind_col = "n_industries" if _has_column(files, "n_industries") else "NULL::BIGINT"
    con.execute(
        "CREATE TEMP TABLE tf AS SELECT section, period, term, doc_count, total_docs, "
        f"n_entities, {ind_col} AS n_industries "
        "FROM read_parquet(?, union_by_name = true)",
        [[str(f) for f in files]],
    )
    # Totales por sección y subperiodo (una fila por subperiodo con documentos).
    tot = con.execute(
        "SELECT section, period, max(total_docs) FROM tf GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    if not tot:
        return TermMatrix([], [], np.zeros((0, 0)), np.zeros((0, 0)), {})
    sub = sub_frequency(tot[0][1])
    need = SUBPERIODS[(sub, grid)]
    by_grid: dict[tuple[str, str], list[str]] = {}
    for section, period, _ in tot:
        by_grid.setdefault((section, to_grid(period, grid)), []).append(period)
    # Periodos completos de la grilla, comunes a todas las secciones (una sola subfamilia).
    per_section: dict[str, set[str]] = {}
    for (section, gp), subs in by_grid.items():
        if len(subs) == need:
            per_section.setdefault(section, set()).add(gp)
    sections = {s for s, _, _ in tot}
    complete = sorted(
        set.intersection(*(per_section.get(s, set()) for s in sections)) if sections else set()
    )
    con.execute("CREATE TEMP TABLE sp AS SELECT DISTINCT section, period, total_docs FROM tf")
    n_sub = con.execute("SELECT count(*) FROM (SELECT DISTINCT period FROM tf)").fetchone()[0]

    # Palabras funcionales desde los datos: unigramas casi omnipresentes.
    con.execute(
        """
        CREATE TEMP TABLE stop AS
        SELECT section, term FROM (
          SELECT tf.section, tf.term,
                 median(tf.doc_count / tf.total_docs) AS med, count(*) AS n
          FROM tf WHERE position(' ' IN tf.term) = 0 GROUP BY 1, 2)
        WHERE med >= ? AND n >= ? / 2
        """,
        [max_median_share, n_sub],
    )
    n_stop = con.execute("SELECT count(*) FROM stop").fetchone()[0]
    # Prevalencia media sobre toda la historia (con la censura en cero: solo para filtrar).
    con.execute(
        """
        CREATE TEMP TABLE keep AS
        WITH share AS (
          SELECT tf.section, tf.term, sum(tf.doc_count) AS c FROM tf GROUP BY 1, 2),
        tot AS (SELECT section, sum(total_docs) AS n FROM sp GROUP BY 1)
        SELECT share.section, share.term, share.c / tot.n AS mean_share
        FROM share JOIN tot USING (section)
        WHERE share.c / tot.n >= ?
          AND NOT EXISTS (SELECT 1 FROM stop s WHERE s.section = share.section AND (
                s.term = split_part(share.term, ' ', 1)
                OR s.term = regexp_extract(share.term, '(\\S+)$', 1)))
        """,
        [min_mean_share],
    )
    n_narrow = n_unmeasured = 0
    if min_entities > 0 and recent > 0 and complete:
        recent_grid = set(complete[-recent:])
        recent_subs = sorted({p for _, p, _ in tot if to_grid(p, grid) in recent_grid})
        con.execute(
            """
            CREATE TEMP TABLE breadth AS
            SELECT k.section, k.term, median(tf.n_entities) AS amp
            FROM keep k LEFT JOIN tf ON tf.section = k.section AND tf.term = k.term
              AND tf.period IN (SELECT unnest(?))
            GROUP BY 1, 2
            """,
            [recent_subs],
        )
        n_narrow, n_unmeasured = con.execute(
            "SELECT count(*) FILTER (amp < ?), count(*) FILTER (amp IS NULL) FROM breadth",
            [min_entities],
        ).fetchone()
        con.execute(
            "DELETE FROM keep WHERE EXISTS (SELECT 1 FROM breadth b WHERE b.section = "
            "keep.section AND b.term = keep.term AND b.amp < ?)",
            [min_entities],
        )
    n_all = con.execute("SELECT count(*) FROM (SELECT DISTINCT section, term FROM tf)").fetchone()[
        0
    ]
    rows = con.execute("SELECT section, term, mean_share FROM keep ORDER BY 1, 2").fetchall()
    n_prefilter = len(rows)
    function_words = {r[0] for r in con.execute("SELECT DISTINCT term FROM stop").fetchall()}
    ok = specific_mask(
        [r[1] for r in rows],
        np.array([r[2] for r in rows]),
        function_words,
        list(boilerplate),
        unigram_max_share,
    )
    rows = [r for r, good in zip(rows, ok, strict=True) if good]
    n_specific = n_prefilter - len(rows)
    keys = [(r[0], r[1]) for r in rows]
    mean_share = np.array([r[2] for r in rows])
    industries = np.full(len(keys), np.nan)
    n_few_ind = 0
    if keys and complete and recent > 0:
        recent_grid = set(complete[-recent:])
        recent_subs = sorted({p for _, p, _ in tot if to_grid(p, grid) in recent_grid})
        got = {
            (sec, term): amp
            for sec, term, amp in con.execute(
                "SELECT section, term, median(n_industries) FROM tf "
                "WHERE period IN (SELECT unnest(?)) AND n_industries IS NOT NULL GROUP BY 1, 2",
                [recent_subs],
            ).fetchall()
        }
        industries = np.array([got.get(k, np.nan) for k in keys], dtype=float)
        if min_industries > 0:
            few = np.nan_to_num(industries, nan=np.inf) < min_industries
            n_few_ind = int(few.sum())
            keys = [k for k, f in zip(keys, few, strict=True) if not f]
            mean_share, industries = mean_share[~few], industries[~few]
    last_doc = None
    if docs_files:
        last_doc = con.execute(
            "SELECT max(date) FROM read_parquet(?, union_by_name = true)",
            [[str(f) for f in docs_files]],
        ).fetchone()[0]
    excluded = {
        "candidatos": int(n_all),
        "palabras_funcionales": int(n_stop),
        "excluidos_prefiltro": int(n_all - len(keys)),
        "excluidos_especificidad": int(n_specific),
        "excluidos_pocas_industrias": n_few_ind,
        "excluidos_amplitud": int(n_narrow),
        "sin_medida_amplitud": int(n_unmeasured),
    }
    if not keys or not complete:
        con.close()
        return TermMatrix(
            complete,
            [],
            np.zeros((0, len(complete))),
            np.zeros((0, 0)),
            excluded,
            last_doc_date=last_doc,
        )

    # Celdas (término × subperiodo) con la censura imputada en el tope, agregadas a la grilla.
    cells = con.execute(
        """
        SELECT k.section, k.term, sp.period, coalesce(tf.doc_count, ?) AS c, sp.total_docs
        FROM keep k JOIN sp USING (section)
        LEFT JOIN tf ON tf.section = k.section AND tf.term = k.term AND tf.period = sp.period
        """,
        [min_doc_count - 1],
    ).fetchall()
    con.close()
    row = {k: i for i, k in enumerate(keys)}
    col = {g: j for j, g in enumerate(complete)}
    counts = np.zeros((len(keys), len(complete)))
    totals = np.zeros_like(counts)
    for section, term, period, c, n in cells:
        j = col.get(to_grid(period, grid))
        if j is None:
            continue
        i = row.get((section, term))
        if i is None:  # excluido por especificidad o amplitud
            continue
        counts[i, j] += min(c, n)
        totals[i, j] += n
    return TermMatrix(complete, keys, counts, totals, excluded, mean_share, industries, last_doc)


def _has_column(files: Sequence[Path], name: str) -> bool:
    import pyarrow.parquet as pq

    return any(name in pq.read_schema(f).names for f in files)


def logit_share(counts: np.ndarray, totals: np.ndarray) -> np.ndarray:
    """logit de la proporción con suavizado de Jeffreys (+0,5 / +1)."""
    p = (counts + 0.5) / (totals + 1.0)
    return np.log(p / (1.0 - p))


# --- Estadístico y prueba ----------------------------------------------------------------------


def emerging_stat(x: np.ndarray, window: int, persistence: int, recent: int) -> np.ndarray:
    """T = min de los últimos `persistence` periodos menos la media de los `window` periodos
    previos a la ventana reciente (`recent` >= `persistence` periodos)."""
    last = x[..., -persistence:]
    prior = x[..., -(recent + window) : -recent]
    return last.min(axis=-1) - prior.mean(axis=-1)


@dataclass(frozen=True)
class TermTestParams:
    window: int
    persistence: int
    recent: int
    phi_margin: float
    tail: TailConfig


def term_test(
    x: np.ndarray, idx: np.ndarray, params: TermTestParams
) -> tuple[float, float, str, int]:
    """(T, p, método, L) de un término; `idx` (B, W + R + burn-in) sobre la historia previa a la
    ventana reciente R: así un crecimiento que empezó dentro de R no contamina el nulo.

    L es el quiebre estimado (largo del segmento elevado). El máximo sobre L se calcula igual en
    el nulo, de modo que la selección del quiebre no infla el p."""
    k, r = params.persistence, params.recent
    t_arr, l_arr = emerging_stat_break(x, params.window, k, r)
    t_obs, length = float(t_arr), int(l_arr)
    if t_obs <= 0:
        return t_obs, 1.0, "empirical", length
    base = x[:-r]
    null = ar_null(base, idx, fit_ar1(base, params.phi_margin))
    t_null = emerging_stat_break(null, params.window, k, r)[0]
    p, method = bootstrap_p(t_obs, t_null, params.tail)
    return t_obs, float(p), method, length


_IDX: np.ndarray | None = None
_PARAMS: TermTestParams | None = None


def _init_worker(idx: np.ndarray, params: TermTestParams) -> None:
    global _IDX, _PARAMS
    _IDX, _PARAMS = idx, params


def _test_chunk(xs: np.ndarray) -> list[tuple[float, float, str, int]]:
    return [term_test(x, _IDX, _PARAMS) for x in xs]


def run_family(
    x: np.ndarray,
    params: TermTestParams,
    n_resamples: int,
    block_setting: int | str,
    seed: int,
    source: str,
    workers: int = 1,
    block_sample: int = 5000,
) -> tuple[list[tuple[float, float, str, int]], float]:
    """Prueba todas las filas de `x` (K, G) con una sola matriz de índices compartida."""
    k = params.recent
    base_len = x.shape[1] - k
    rows = list(range(x.shape[0]))
    if len(rows) > block_sample:  # mediana de las longitudes óptimas sobre una muestra fija
        rows = sorted(random.Random(seed).sample(rows, block_sample))
    block = family_block_length([x[i, :-k] for i in rows], block_setting)
    seed_key = (seed, zlib.crc32(f"{DETECTOR}|{source}".encode()), base_len)
    idx = stationary_indices(
        base_len, n_resamples, block, seed_key, n_out=params.window + params.recent + AR_BURN_IN
    )
    if workers <= 1 or x.shape[0] < 200:
        _init_worker(idx, params)
        return _test_chunk(x), block
    chunks = np.array_split(x, workers * 8)
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(workers, ctx, _init_worker, (idx, params)) as pool:
        out = [r for chunk in pool.map(_test_chunk, chunks) for r in chunk]
    return out, block


def priority_scores(
    keys: list[tuple[str, str]],
    effect: np.ndarray,
    mean_share: np.ndarray,
    industries: np.ndarray,
    cfg: dict[str, float],
) -> np.ndarray:
    """Prioridad para la entrega: efecto, con bonus por palabras extra (2–3 palabras antes que
    unigramas), penalizada por la prevalencia base (las palabras comunes no ganan por común) y
    ponderada por la dispersión entre industrias cuando se midió (log relativo al máximo)."""
    if not keys:
        return np.zeros(0)
    n_tok = np.array([len(term.split()) for _, term in keys], dtype=float)
    pr = np.maximum(effect, 0.0) * (1 + cfg["ngram_bonus"] * (n_tok - 1))
    pr = pr / (1 + mean_share / cfg["base_share_ref"])
    if np.isfinite(industries).any():
        top = np.log1p(np.nanmax(industries))
        wide = np.log1p(np.nan_to_num(industries, nan=0.0)) / top if top > 0 else 1.0
        pr = np.where(np.isfinite(industries), pr * (0.5 + 0.5 * wide), pr)
    return pr


# --- Corrida -----------------------------------------------------------------------------------


def detect_source(cfg: dict[str, Any], source: str, run_id: str, started_at: datetime) -> dict:
    """Prueba una fuente, cierra su familia (BH/BY) y escribe tests, signals y detector_runs."""
    td = cfg["text_detect"]
    grid = td["grid"][source]
    window = td["window"][grid]
    persistence = cfg["persistence"]["min_consecutive_periods"][grid]
    recent = max(td["recent"][grid], persistence)
    cal = cfg["fdr"]["calibration"]
    q = float(cfg["fdr"]["q"])
    processed = _resolve(cfg["paths"]["processed_dir"])
    files = sorted((processed / "term_freq" / source).glob("*/*.parquet"))
    if not files:
        raise RuntimeError(f"no hay term_freq de {source}")
    m = load_matrix(
        files,
        grid,
        cfg["text"]["min_doc_count"],
        td["min_mean_share"],
        td["max_median_share"],
        min_entities=td["min_entities"].get(source, 0),
        recent=max(td["recent"][grid], persistence),
        boilerplate=td["boilerplate"],
        unigram_max_share=td["unigram_max_share"],
        min_industries=td["min_sic2"].get(source, 0),
        docs_files=sorted((processed / "text_docs" / source).glob("*/*.parquet")),
    )
    g = len(m.periods)
    if g < window + recent + 3:
        raise RuntimeError(
            f"{source}: {g} periodos completos en la grilla {grid}; se necesitan al menos "
            f"{window + recent + 3} (ventana previa + reciente + línea base)"
        )
    x = logit_share(m.counts, m.totals)
    # Deriva del corpus: cada término contra la mediana de los de prevalencia base parecida.
    if td["drift"]["bins"] > 0:  # bins = 0 desactiva la corrección (solo para comparar)
        x, _ = drift_adjust(x, slice(0, g - recent), td["drift"]["bins"], td["drift"]["min_bin"])
    params = TermTestParams(
        window,
        persistence,
        recent,
        td["phi_margin"],
        TailConfig(**cfg["detect"]["tail"]),
    )
    results, block = run_family(
        x,
        params,
        cal["n_resamples"],
        cal["block_length"],
        cal["seed"],
        source,
        workers=td["workers"],
        block_sample=td["block_sample"],
    )
    p = np.array([r[1] for r in results])
    q_bh, q_by = bh_adjust(p), by_adjust(p)
    # La ventana nunca termina después del último documento de la fuente.
    w_end = grid_end(m.periods[-1])
    if m.last_doc_date is not None:
        w_end = min(w_end, m.last_doc_date)
    run_date = datetime.now(UTC).date()
    tests, signals = [], []
    for (section, term), (t, pv, method, length), a, b in zip(
        m.keys, results, q_bh, q_by, strict=True
    ):
        object_id = f"{section}|{term}"
        w_start = min(grid_start(m.periods[g - length]), w_end)  # quiebre estimado por término
        tests.append(
            {
                "run_id": run_id,
                "detector": DETECTOR,
                "source": source,
                "object_id": object_id,
                "frequency": grid,
                "grid": grid,
                "n_growth": g,
                "block_length": block,
                "statistic": t,
                "effect_size": t,  # en unidades de logit de la proporción
                "persistent": t > 0,
                "p_value": pv,
                "p_method": method,
                "q_value": float(a),
                "q_by": float(b),
                "n_tests": len(results),
                "window_start": w_start,
                "window_end": w_end,
                "copyrighted": False,
            }
        )
        if a <= q:
            signals.append(
                {
                    "signal_id": signal_id(run_id, DETECTOR, source, object_id),
                    "source": source,
                    "object_id": object_id,
                    "detector": DETECTOR,
                    "window_start": w_start,
                    "window_end": w_end,
                    "effect_size": t,
                    "p_value": pv,
                    "q_value": float(a),
                    "run_date": run_date,
                    "run_id": run_id,
                    "n_tests": len(results),
                }
            )
    n_by = int(np.sum(q_by <= q))
    n_ind = {k: v for k, v in zip(m.keys, m.industries, strict=True)} if len(m.keys) else {}
    prio = priority_scores(
        m.keys,
        np.array([r[0] for r in results]),
        m.mean_share,
        m.industries,
        td["priority"],
    )
    ac = cfg["fdr"]["approval_alert"]
    others = [
        {"detector": f"{r['family']}/{r['detector']}", "n_bh": r["n_bh"], "n_tests": r["n_tests"]}
        for r in latest_runs(cfg, ["numeric", "fred", *td["grid"]])
        if r["family"] != source
    ]
    me = {"detector": f"{source}/{DETECTOR}", "n_bh": len(signals), "n_tests": len(results)}
    alert = alert_flags([me, *others], ac["factor"], ac["min_rate"])[me["detector"]]
    params_json = {
        "approval_alert": alert,
        "bh_rate": len(signals) / len(results) if results else 0.0,
        "grid": grid,
        "window": window,
        "persistence": persistence,
        "recent": recent,
        "periods": [m.periods[0], m.periods[-1]] if m.periods else [],
        "block_length": block,
        "text_detect": td,
        "min_doc_count": cfg["text"]["min_doc_count"],
        "calibration": cal,
        "prefiltro": m.excluded,
    }
    runs = [
        {
            "run_id": run_id,
            "detector": DETECTOR,
            "started_at": started_at.replace(microsecond=0),
            "finished_at": datetime.now(UTC).replace(microsecond=0),
            "n_tests": len(results),
            "n_bh": len(signals),
            "n_by": n_by,
            "q": q,
            "params_json": json.dumps(params_json, sort_keys=True, default=str),
        }
    ]
    if tests:
        write(DETECTOR_TESTS, tests, table_root(cfg, "detector_tests"), source, "object_id")
    if signals:
        write(SIGNALS, signals, table_root(cfg, "signals"), source, "object_id")
    write(DETECTOR_RUNS, runs, table_root(cfg, "detector_runs"), source, "run_id")
    by_key = {f"{sec}|{term}": float(pr) for (sec, term), pr in zip(m.keys, prio, strict=True)}
    top = sorted(signals, key=lambda s: s["q_value"])[:20]
    ranked = sorted(signals, key=lambda s: -by_key[s["object_id"]])[: td["top_n"]]
    return {
        "source": source,
        "run_id": run_id,
        "grid": grid,
        "periods": g,
        "n_tests": len(results),
        "n_bh": len(signals),
        "n_by": n_by,
        "block_length": block,
        "prefiltro": m.excluded,
        "bh_rate": len(signals) / len(results) if results else 0.0,
        "approval_alert": alert,
        "top": [(s["object_id"], round(s["effect_size"], 3), s["q_value"]) for s in top],
        "top_priority": [
            {
                "object_id": s["object_id"],
                "effect_size": round(s["effect_size"], 3),
                "priority": round(by_key[s["object_id"]], 3),
                "q_value": s["q_value"],
                "window_start": s["window_start"],
                "window_end": s["window_end"],
                "n_industries": None
                if np.isnan(n_ind.get(tuple(s["object_id"].split("|", 1)), np.nan))
                else float(n_ind[tuple(s["object_id"].split("|", 1))]),
            }
            for s in ranked
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.detect.text")
    parser.add_argument("--source", required=True, choices=["edgar", "federal_register"])
    args = parser.parse_args(argv)
    cfg = load_config()
    log = get_logger("detect.text", cfg["logging"]["level"])
    with run([args.source], _resolve(cfg["logging"]["runs_file"])) as record:
        with record.stage("detect_text"):
            report = detect_source(cfg, args.source, record.run_id, record.started_at)
        record.n_series[args.source] = report["n_tests"]
        record.details = {"command": "detect text", "report": report}
        log.info(
            "familia cerrada", extra={"fields": {k: v for k, v in report.items() if k != "top"}}
        )
    print(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    return 0


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


if __name__ == "__main__":
    sys.exit(main())
