"""Capa 2 del plan de segunda etapa: las 9 dimensiones como tabla de cifras (ADR 0029).

Por fenómeno (candidato o en observación) y por industria: cifras con valor, unidad, periodo,
fuente y nivel. Sin IA y sin conclusiones redactadas. Nunca filtra ni cambia el ranking.

Niveles de cada cifra (`level`):
- `directo` (✅): dato del nivel NAICS del fenómeno;
- `indirecto` (🟡): dato de otro nivel NAICS (se sube de 6 a 4 a 3 dígitos; o se baja a una
  subindustria para el índice de precios) o dato que no es de la industria misma;
- `sin_dato` (⚪): no hay dato; la fila existe igual, con la dimensión y la métrica que faltan.

La industria sale de `rank_phenomena.naics_json`: el código MÁS ESPECÍFICO cuyo peso es al menos
`rank.dimensions.naics_min_weight_share` del mayor. El índice de precios del productor se encuentra
por el código NAICS embebido en el id (`naics.ID_FORMATS`), nunca por una lista de series.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.rank import naics as naics_mod
from src.rank import trajectory as traj_mod
from src.schemas import DIMENSION_NAMES

DIMENSIONS = DIMENSION_NAMES
LEVELS = ("directo", "indirecto", "sin_dato")
LEVEL_MARK = {"directo": "✅", "indirecto": "🟡", "sin_dato": "⚪"}
PPI_ID = re.compile(r"^PCU(\d{2}-\d{2}(?=-)|\d{2,6})")


@dataclass(frozen=True)
class DimParams:
    naics_min_weight_share: float = 0.5
    risk_section_regex: str = r"(?i)item\s*1a"
    risk_max_items: int = 5
    horizon_years: int = 7
    size_cr: tuple[int, ...] = (4, 8, 20, 50)


def params_from(cfg: Mapping[str, Any]) -> DimParams:
    d = cfg["rank"]["dimensions"]
    return DimParams(
        float(d["naics_min_weight_share"]),
        str(d["risk_section_regex"]),
        int(d["risk_max_items"]),
        int(d["horizon_years"]),
        tuple(int(x) for x in d["concentration_ratios"]),
    )


def pick_naics(naics: Sequence[Sequence[Any]], min_share: float) -> str | None:
    """Código más específico entre los de peso ≥ `min_share` del mayor; None sin anclaje."""
    if not naics:
        return None
    top = max(float(w) for _, w in naics)
    ok = [str(c) for c, w in naics if float(w) >= min_share * top]
    return max(ok, key=lambda c: (len(c.replace("-", "")), c)) if ok else None


def ladder(code: str) -> list[str]:
    """Código y sus ancestros, del más específico al más amplio (6 → 4 → 3 → 2 dígitos)."""
    if "-" in code:
        return [code]
    out = [code]
    for n in (4, 3, 2):
        if len(code) > n and code[:n] not in out:
            out.append(code[:n])
    return out


class Market:
    """`market/naics_metrics` en memoria: (naics, metric) -> {año: (valor, unidad, serie)}."""

    def __init__(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self.data: dict[tuple[str, str], dict[int, tuple[float, str | None, str | None]]] = (
            defaultdict(dict)
        )
        for r in rows:
            self.data[(r["naics"], r["metric"])][int(r["year"])] = (
                float(r["value"]),
                r.get("units"),
                r.get("series_id") or None,
            )

    def latest(
        self, code: str, metric: str, year: int | None = None
    ) -> tuple[int, float, str | None, str | None] | None:
        s = self.data.get((code, metric))
        if not s:
            return None
        y = year if year is not None else max(s)
        if y not in s:
            return None
        v, u, sid = s[y]
        return y, v, u, sid

    def climb(
        self, code: str, metric: str
    ) -> tuple[str, int, float, str | None, str | None] | None:
        """Dato más reciente de la métrica en el nivel más específico que lo tenga."""
        for c in ladder(code):
            if (hit := self.latest(c, metric)) is not None:
                return (c, *hit)
        return None


@dataclass
class Row:
    dimension: str
    metric: str
    value: float | None = None
    value_text: str | None = None
    unit: str | None = None
    period: str | None = None
    source_ref: str | None = None
    level: str = "sin_dato"
    naics_used: str | None = None
    public: bool = True  # False: la serie de origen tiene copyright (no se muestra su id)


def _level(target: str, used: str) -> str:
    return "directo" if used == target else "indirecto"


def _missing(dim: str, metric: str, target: str | None) -> Row:
    return Row(dim, metric, naics_used=target)


def market_row(market: Market, target: str, dim: str, metric: str, name: str | None = None) -> Row:
    hit = market.climb(target, metric)
    if hit is None:
        return _missing(dim, name or metric, target)
    code, year, value, unit, sid = hit
    return Row(
        dim,
        name or metric,
        value,
        None,
        unit,
        str(year),
        f"market/naics_metrics:{metric}" + (f" ({sid})" if sid else ""),
        _level(target, code),
        code,
    )


def ratio_row(
    market: Market, target: str, dim: str, name: str, num: str, den: str, unit: str
) -> Row:
    """num/den en el mismo año y en el mismo código NAICS (el más específico que tenga ambos)."""
    for c in ladder(target):
        a, b = market.data.get((c, num)), market.data.get((c, den))
        if a and b and (years := sorted(set(a) & set(b))):
            y = years[-1]
            if b[y][0]:
                return Row(
                    dim,
                    name,
                    a[y][0] / b[y][0],
                    None,
                    unit,
                    str(y),
                    f"market/naics_metrics:{num}/{den}",
                    _level(target, c),
                    c,
                )
    return _missing(dim, name, target)


# --- precios -------------------------------------------------------------------------------------


def find_price_series(
    series: Iterable[tuple[str, str]], tables: naics_mod.NaicsTables
) -> dict[str, tuple[str, str]]:
    """NAICS -> (fuente, id) del índice de precios del productor de la industria completa.

    Lee el código embebido en el id (`PCU<industria><producto>`); la serie de la industria completa
    es la que repite el código como producto (`PCU623110623110`). Empate entre fuentes: la primera
    en orden alfabético."""
    out: dict[str, tuple[str, str]] = {}
    for source, sid in sorted(series):
        m = PPI_ID.match(sid or "")
        if m is None or source not in ("bls", "fred"):
            continue
        rest = sid[3:]
        half, odd = divmod(len(rest), 2)
        if odd or rest[:half] != rest[half:]:
            continue  # no es el agregado de la industria (lleva un producto distinto)
        codes = naics_mod.id_codes(source, sid, tables)
        if codes:
            out.setdefault(codes[0], (source, sid))
    return out


def price_source(
    target: str, prices: Mapping[str, tuple[str, str]]
) -> tuple[str, tuple[str, str]] | None:
    """Mismo código, luego ancestros, luego la subindustria con el código más corto (más amplia)."""
    for c in ladder(target):
        if c in prices:
            return c, prices[c]
    sub = sorted((c for c in prices if c.startswith(target)), key=lambda c: (len(c), c))
    return (sub[0], prices[sub[0]]) if sub else None


def change(
    dates: Sequence[date], values: Sequence[float], base: date
) -> tuple[date, date, float] | None:
    """Cambio porcentual entre la primera observación en/tras `base` y la última."""
    pts = [(d, v) for d, v in zip(dates, values, strict=True) if d >= base and v]
    if len(pts) < 2:
        return None
    (d0, v0), (d1, v1) = pts[0], pts[-1]
    return d0, d1, 100.0 * (v1 / v0 - 1.0)


def base_date(
    last: date, recession_months: Sequence[tuple[int, float]], p: traj_mod.TrajectoryParams
) -> date:
    """Inicio de la recesión más reciente dentro de `max_back_years`; si no hay, `recent_years`
    antes del último dato (mismo ancla que la trayectoria)."""
    mi = last.year * 12 + last.month - 1
    starts = [
        s
        for s in traj_mod.recession_starts(recession_months, "M")
        if s < mi and mi - s <= p.max_back_years * 12
    ]
    m = max(starts) if starts else mi - round(p.recent_years * 12)
    return date(m // 12, m % 12 + 1, 1)


def level_vs_peak(
    dates: Sequence[date],
    values: Sequence[float],
    recession_months: Sequence[tuple[int, float]],
    p: traj_mod.TrajectoryParams,
) -> tuple[date, float] | None:
    """(fecha del pico, % del último nivel frente al máximo de los `peak_years` previos a la última
    recesión). None sin recesión en el rango o sin datos."""
    if not values:
        return None
    anchor = base_date(dates[-1], recession_months, p)
    lo = date(anchor.year - round(p.peak_years), anchor.month, 1)
    pts = [(d, v) for d, v in zip(dates, values, strict=True) if lo <= d <= anchor and v]
    if not pts:
        return None
    dpk, peak = max(pts, key=lambda t: t[1])
    return dpk, 100.0 * (values[-1] / peak - 1.0)


# --- construcción --------------------------------------------------------------------------------

SeriesFn = Callable[[str, str], "tuple[list[date], list[float]] | None"]


def build_phenomenon_rows(
    phen: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    market: Market,
    *,
    series: SeriesFn,
    deflator: tuple[list[date], list[float]] | None,
    deflator_id: str | None,
    prices: Mapping[str, tuple[str, str]],
    recession_months: Sequence[tuple[int, float]],
    traj_params: traj_mod.TrajectoryParams,
    terms: Sequence[Mapping[str, Any]],
    p: DimParams,
) -> list[Row]:
    """Filas de las 9 dimensiones de un fenómeno. `evidence`: filas de `rank_evidence` de sus
    clusters; `terms`: términos de texto con NAICS ({object_id, naics, peso, q_value, ...})."""
    target = pick_naics(json.loads(phen["naics_json"]), p.naics_min_weight_share)
    rows: list[Row] = []
    ser = sorted(
        (e for e in evidence if e["item_kind"] == "series"),
        key=lambda e: (e["q_value"], e["source"], e["object_id"]),
    )

    def ref(e: Mapping[str, Any]) -> str | None:
        return f"{e['source']}:{e['object_id']}" if e["public"] else None

    # Fenómeno
    rows.append(
        Row(
            "fenomeno",
            "direccion",
            value_text=phen["direction"],
            period=f"{phen['window_start']} a {phen['window_end']}",
            source_ref="rank_phenomena",
            level="directo",
            naics_used=target,
        )
    )
    if phen.get("trajectory"):
        rows.append(
            Row(
                "fenomeno",
                "trayectoria",
                value_text=phen["trajectory"],
                source_ref="rank_phenomena.trajectory",
                level="directo",
                naics_used=target,
            )
        )
    rows.append(
        Row(
            "fenomeno",
            "industria",
            value_text=target,
            source_ref="rank_phenomena.naics_json",
            level="directo" if target else "sin_dato",
            naics_used=target,
        )
    )
    # Evidencia (cada serie: q, efecto, clase, trayectoria)
    for e in ser:
        base = f"serie:{e['source']}:{e['object_id']}" if e["public"] else None
        tag = base or f"serie_con_derechos:{e['source']}:{_anon(e['object_id'])}"
        rows.append(
            Row(
                "evidencia",
                f"q:{tag}",
                e["q_value"],
                unit="q (BH)",
                period=f"{e.get('window_start')} a {e.get('window_end')}",
                source_ref=ref(e),
                level="directo",
                naics_used=target,
                public=bool(e["public"]),
            )
        )
        rows.append(
            Row(
                "evidencia",
                f"efecto:{tag}",
                e["effect_size"],
                value_text=" / ".join(x for x in (e.get("measure_kind"), e.get("trajectory")) if x)
                or None,
                unit="efecto estandarizado",
                source_ref=ref(e),
                level="directo",
                naics_used=target,
                public=bool(e["public"]),
            )
        )
    if not ser:
        rows.append(_missing("evidencia", "series", target))

    # Demanda: efecto real (valor) o efecto (cantidad) de las series del fenómeno
    demand = [e for e in ser if e.get("measure_kind") in ("valor", "cantidad")]
    for e in demand:
        real = e.get("measure_kind") == "valor"
        val = e.get("real_effect") if real else e["effect_size"]
        tag = (
            f"{e['source']}:{e['object_id']}"
            if e["public"]
            else f"{e['source']}:(derechos:{_anon(e['object_id'])})"
        )
        rows.append(
            Row(
                "demanda",
                ("crecimiento_real:" if real else "crecimiento_cantidad:") + tag,
                val,
                unit="efecto estandarizado" + (" (deflactado)" if real else ""),
                period=f"{e.get('window_start')} a {e.get('window_end')}",
                source_ref=ref(e),
                level="directo" if val is not None else "sin_dato",
                naics_used=target,
                public=bool(e["public"]),
            )
        )
    if not demand:
        rows.append(_missing("demanda", "crecimiento_real", target))
    rows.append(
        market_row(market, target, "demanda", "revenue", "ingresos_industria")
        if target
        else _missing("demanda", "ingresos_industria", target)
    )

    # Brecha: precio frente al deflactor y nivel frente al pico previo a la recesión
    rows.append(
        _price_gap(target, prices, series, deflator, deflator_id, recession_months, traj_params)
    )
    peaks = 0
    for e in ser:
        if e.get("measure_kind") != "cantidad":
            continue
        got = series(e["source"], e["object_id"])
        lp = level_vs_peak(*got, recession_months, traj_params) if got else None
        if lp is None:
            continue
        peaks += 1
        rows.append(
            Row(
                "brecha",
                f"nivel_vs_pico_previo:{e['source']}:{e['object_id']}"
                if e["public"]
                else f"nivel_vs_pico_previo:{e['source']}:(derechos:{_anon(e['object_id'])})",
                lp[1],
                value_text=f"pico {lp[0]:%Y-%m}",
                unit="% frente al máximo previo a la última recesión",
                period=f"{got[0][-1]:%Y-%m}",
                source_ref=ref(e),
                level="directo",
                naics_used=target,
                public=bool(e["public"]),
            )
        )
    if not peaks:
        rows.append(_missing("brecha", "nivel_vs_pico_previo", target))

    # Competencia, dinero, entrada (market/naics_metrics)
    for metric, name in (
        ("firms", "empresas"),
        ("establishments", "establecimientos"),
        *((f"cr{n}", f"cr{n}") for n in p.size_cr),
        ("small_firm_share", "empresas_pequenas"),
    ):
        rows.append(
            market_row(market, target, "competencia", metric, name)
            if target
            else _missing("competencia", name, target)
        )
    rows.append(
        market_row(market, target, "dinero", "revenue", "ingresos_totales")
        if target
        else _missing("dinero", "ingresos_totales", target)
    )
    if target:
        rows.append(
            ratio_row(
                market,
                target,
                "dinero",
                "ingreso_por_establecimiento",
                "revenue",
                "establishments_ec",
                "ingresos/establecimiento",
            )
        )
        rows.append(
            ratio_row(
                market,
                target,
                "dinero",
                "ingreso_por_empresa",
                "revenue",
                "firms",
                "ingresos/empresa",
            )
        )
    else:
        rows += [
            _missing("dinero", m, target)
            for m in ("ingreso_por_establecimiento", "ingreso_por_empresa")
        ]
    for metric in ("entry_rate", "exit_rate"):
        r = (
            market_row(market, target, "entrada", metric)
            if target
            else _missing("entrada", metric, target)
        )
        rows.append(r)
        econ = market.latest("00", metric, int(r.period)) if r.period else None
        if r.value is not None and econ is not None:
            rows.append(
                Row(
                    "entrada",
                    f"{metric}_economia_total",
                    econ[1],
                    unit=econ[2],
                    period=str(econ[0]),
                    source_ref=f"market/naics_metrics:{metric}",
                    level="directo",
                    naics_used="00",
                )
            )
            rows.append(
                Row(
                    "entrada",
                    f"{metric}_menos_economia",
                    r.value - econ[1],
                    unit="puntos porcentuales",
                    period=r.period,
                    source_ref=f"market/naics_metrics:{metric}",
                    level=r.level,
                    naics_used=r.naics_used,
                )
            )
        else:
            rows.append(_missing("entrada", f"{metric}_economia_total", "00"))

    # Riesgo: términos de la sección de riesgos con empresas de la industria
    rows += _risk_rows(target, terms, p)

    # Confianza: conteo de niveles de las demás dimensiones
    counts = {lv: sum(r.level == lv for r in rows) for lv in LEVELS}
    for lv in LEVELS:
        rows.append(
            Row(
                "confianza",
                f"n_{lv}",
                float(counts[lv]),
                value_text=LEVEL_MARK[lv],
                unit="cifras",
                source_ref="phenomenon_dimensions",
                level="directo",
                naics_used=target,
            )
        )
    return rows


def _price_gap(
    target: str | None,
    prices: Mapping[str, tuple[str, str]],
    series: SeriesFn,
    deflator: tuple[list[date], list[float]] | None,
    deflator_id: str | None,
    recession_months: Sequence[tuple[int, float]],
    tp: traj_mod.TrajectoryParams,
) -> Row:
    name = "precio_industria_vs_deflactor"
    if not target or not deflator or not deflator[0]:
        return _missing("brecha", name, target)
    hit = price_source(target, prices)
    got = series(*hit[1]) if hit else None
    if hit is None or not got or not got[0]:
        return _missing("brecha", name, target)
    code, (src, sid) = hit
    base = base_date(got[0][-1], recession_months, tp)
    ppi, defl = change(*got, base), change(*deflator, base)
    if ppi is None or defl is None:
        return _missing("brecha", name, target)
    return Row(
        "brecha",
        name,
        ppi[2] - defl[2],
        value_text=f"precio {ppi[2]:+.1f}% · deflactor {defl[2]:+.1f}%",
        unit="puntos porcentuales",
        period=f"{ppi[0]:%Y-%m} a {ppi[1]:%Y-%m}",
        source_ref=f"{src}:{sid} vs fred:{deflator_id}",
        level=_level(target, code),
        naics_used=code,
    )


def _risk_rows(target: str | None, terms: Sequence[Mapping[str, Any]], p: DimParams) -> list[Row]:
    if not target:
        return [_missing("riesgo", "terminos_item_1a", target)]
    sec = re.compile(p.risk_section_regex)
    ok = ladder(target)
    pool: dict[str, tuple[float, str, Mapping[str, Any]]] = {}
    for t in terms:
        section = str(t["object_id"]).split("|", 1)[0]
        if not sec.search(section):
            continue
        for code, w in t["naics"]:
            if code in ok and (
                t["object_id"] not in pool or ok.index(code) < ok.index(pool[t["object_id"]][1])
            ):
                pool[t["object_id"]] = (w, code, t)
    best = sorted(pool.items(), key=lambda kv: (kv[1][2]["q_value"], -kv[1][0], kv[0]))[
        : p.risk_max_items
    ]
    if not best:
        return [_missing("riesgo", "terminos_item_1a", target)]
    out = []
    for oid, (w, code, t) in best:
        out.append(
            Row(
                "riesgo",
                f"termino:{oid.split('|', 1)[-1]}",
                t["q_value"],
                value_text=f"peso NAICS {w:.2f}",
                unit="q (BH)",
                period=f"{t.get('window_start')} a {t.get('window_end')}",
                source_ref=f"{t['source']}:{oid}",
                level=_level(target, code),
                naics_used=code,
            )
        )
    return out


def _anon(object_id: str) -> str:
    """Etiqueta estable que no revela el id de una serie con copyright."""
    return hashlib.sha1(object_id.encode()).hexdigest()[:8]


def clean(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


def to_table_rows(run_id: str, phenomenon_id: str, rows: Iterable[Row]) -> list[dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        out[(r.dimension, r.metric)] = {
            "run_id": run_id,
            "phenomenon_id": phenomenon_id,
            "dimension": r.dimension,
            "metric": r.metric,
            "value": clean(r.value),
            "value_text": r.value_text,
            "unit": r.unit,
            "period": r.period,
            "source_ref": r.source_ref,
            "level": r.level,
            "naics_used": r.naics_used,
            "public": r.public,
        }
    return [out[k] for k in sorted(out)]


# --- carga y escritura ---------------------------------------------------------------------------


def run_dimensions(
    cfg: Mapping[str, Any], run_id: str | None = None, *, con: Any = None
) -> dict[str, Any]:
    """Calcula y escribe `rank/phenomenon_dimensions` de una corrida (por defecto, la última)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from src.catalog import connect
    from src.config import REPO_ROOT
    from src.detect.run import _resolve
    from src.ingest.store import read_table
    from src.rank import run as rank_run_mod
    from src.rank.run import rank_root
    from src.schemas import (
        NAICS_LINKS,
        RANK_EVIDENCE,
        RANK_PHENOMENA,
    )

    part = rank_run_mod.PARTITION
    if run_id is None:
        latest = rank_run_mod.latest_rank_run(dict(cfg))
        if latest is None:
            return {"run_id": None, "phenomena": 0, "rows": 0}
        run_id = latest["run_id"]
    where = {"where": "run_id = ?", "params": [run_id]}
    phens = read_table(
        RANK_PHENOMENA, rank_root(dict(cfg), "rank_phenomena"), source=part, **where
    ).to_pylist()
    phens = [r for r in phens if r["status"] == "candidato" or r.get("watch")]
    ev = read_table(
        RANK_EVIDENCE, rank_root(dict(cfg), "rank_evidence"), source=part, **where
    ).to_pylist()
    links = read_table(
        NAICS_LINKS, rank_root(dict(cfg), "naics_links"), source=part, **where
    ).to_pylist()
    own = con is None
    root = _resolve(cfg["paths"]["processed_dir"])
    con = con or connect(root)
    try:
        tables = naics_mod.load_tables(cfg, REPO_ROOT)
        mfile = root / "market" / "naics_metrics" / "part-0.parquet"
        market = Market(pq.read_table(mfile).to_pylist() if mfile.exists() else [])
        pids = [
            (a, b)
            for a, b in con.execute(
                "SELECT DISTINCT source, series_id FROM series_meta WHERE series_id LIKE 'PCU%'"
            ).fetchall()
        ]
        prices = find_price_series(pids, tables)
        cache: dict[tuple[str, str], Any] = {}

        def series(source: str, sid: str) -> tuple[list[date], list[float]] | None:
            if (source, sid) not in cache:
                r = con.execute(
                    "SELECT list(date ORDER BY date), list(value ORDER BY date) FROM series "
                    "WHERE source = ? AND series_id = ? AND vintage_date IS NULL "
                    "AND value IS NOT NULL",
                    [source, sid],
                ).fetchone()
                cache[(source, sid)] = (list(r[0]), list(r[1])) if r and r[0] else None
            return cache[(source, sid)]

        ref = cfg["context"]["reference_series"]
        deflator = series("fred", ref["deflator"]) if ref.get("deflator") else None
        months = rank_run_mod.recession_months(con, dict(cfg))
        tp = traj_mod.params_from(dict(cfg))
        p = params_from(cfg)
        by_link: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
        for r in links:
            by_link[(r["source"], r["object_id"])].append((r["naics"], r["peso"]))
        terms = [
            {**e, "naics": by_link[(e["source"], e["object_id"])]}
            for e in ev
            if e["item_kind"] == "term" and (e["source"], e["object_id"]) in by_link
        ]
        by_cand: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in ev:
            by_cand[e["candidate_id"]].append(e)
        out: list[dict[str, Any]] = []
        for ph in sorted(phens, key=lambda r: r["phenomenon_id"]):
            evid = [e for c in json.loads(ph["cluster_ids_json"]) for e in by_cand.get(c, [])]
            rows = build_phenomenon_rows(
                ph,
                evid,
                market,
                series=series,
                deflator=deflator,
                deflator_id=ref.get("deflator"),
                prices=prices,
                recession_months=months,
                traj_params=tp,
                terms=terms,
                p=p,
            )
            out += to_table_rows(run_id, ph["phenomenon_id"], rows)
    finally:
        if own:
            con.close()
    from src.ingest.store import upsert
    from src.schemas import PHENOMENON_DIMENSIONS

    table = pa.Table.from_pylist(out, schema=PHENOMENON_DIMENSIONS.schema)
    upsert(
        table,
        PHENOMENON_DIMENSIONS,
        rank_root(dict(cfg), "phenomenon_dimensions"),
        source=part,
        n_buckets=rank_run_mod.N_BUCKETS,
        bucket_col="run_id",
    )
    return {
        "run_id": run_id,
        "phenomena": len(phens),
        "rows": len(out),
        "market_loaded": bool(market.data),
        "levels": {lv: sum(r["level"] == lv for r in out) for lv in LEVELS},
    }
