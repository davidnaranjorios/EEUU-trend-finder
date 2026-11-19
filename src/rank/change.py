"""Capa 3, pregunta 1: ¿qué cambió realmente? (ADR 0030). Reglas fijas, sin IA.

Por fenómeno (candidato o en observación) clasifica cinco tipos de cambio, cada uno `si`, `no` o
`sin_dato`, con las cifras que lo deciden y el umbral usado, y una fila auxiliar `recuperacion`:

- cantidad: series de cantidad no poblacional, o de valor con efecto real positivo, cuya
  trayectoria es `acelera` o `gira_al_alza`. `frena_caida` y `rebote_bajo_pico` NO cuentan: se
  reportan como recuperación;
- precio: el índice de precios del productor de la industria crece más que el deflactor aprobado
  en la ventana del fenómeno, por encima de un umbral (pp por año);
- productividad: el ingreso real (deflactado) por trabajador o por establecimiento sube más que el
  umbral entre censos económicos mientras el empleo está estable o cae;
- composición: al menos uno de peso de subindustrias, CR4/CR8, % de empresas pequeñas, o tasa de
  entrada/salida frente a la economía total;
- regulación: no hay vínculo entre documentos del Registro Federal e industria; queda `sin_dato`.

Solo describe: no filtra ni toca detectores, FDR, ranking ni dimensiones. Los umbrales viven en
`config.yaml` (`rank.change`); la industria y las series salen de los datos.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import numpy as np

from src.rank import dimensions as dm
from src.rank import role as role_mod
from src.rank import trajectory as traj_mod
from src.schemas import ChangeType

COUNTS = ("acelera", "gira_al_alza")  # etiquetas de trayectoria que cuentan como «creció más»
RECOVERY = ("frena_caida", "rebote_bajo_pico")
NO_REGULATION_NOTE = "falta vincular regulación con industria"
MAIN = ("cantidad", "precio", "productividad", "composicion", "regulacion")
SI, NO, SD = "si", "no", "sin_dato"


@dataclass(frozen=True)
class ChangeParams:
    price_min_pp_year: float = 1.0
    productivity_min_pct_year: float = 1.0
    employment_stable_pct_year: float = 1.0
    subindustry_shift_pp: float = 5.0
    concentration_shift_pp: float = 2.0
    small_share_shift_pp: float = 2.0
    entry_exit_gap_pp: float = 2.0
    composition_years: int = 7


def params_from(cfg: Mapping[str, Any]) -> ChangeParams:
    c = cfg["rank"]["change"]
    return ChangeParams(
        float(c["price_min_pp_year"]),
        float(c["productivity_min_pct_year"]),
        float(c["employment_stable_pct_year"]),
        float(c["child_naics_shift_pp"]),
        float(c["concentration_shift_pp"]),
        float(c["small_share_shift_pp"]),
        float(c["entry_exit_gap_pp"]),
        int(c["composition_years"]),
    )


@dataclass
class Out:
    change_type: str
    status: str = SD
    value: float | None = None
    unit: str | None = None
    threshold: str | None = None
    period: str | None = None
    source_ref: str | None = None
    note: str | None = None
    level: str | None = None  # directo ✅ / indirecto 🟡; None: se deduce del estado


def label(change_type: str) -> str:
    return ChangeType[change_type].value.lower()


def _tag(e: Mapping[str, Any]) -> str:
    return f"{e['source']}:{e['object_id']}" if e["public"] else ""


def _refs(items: Sequence[Mapping[str, Any]]) -> tuple[str | None, int]:
    """Ids de las series públicas y cuántas llevan derechos (no se listan)."""
    pub = sorted(_tag(e) for e in items if e["public"])
    return (", ".join(pub) or None), sum(not e["public"] for e in items)


# --- cantidad y recuperación ----------


@dataclass(frozen=True)
class Ctx:
    """Insumos de datos para cantidad y productividad (todo sale de los datos del fenómeno)."""

    target: str | None
    roles: Mapping[tuple[str, str], str]
    series_naics: Mapping[tuple[str, str], str | None]
    prices: Mapping[str, tuple[str, str]]
    series: dm.SeriesFn
    deflator: tuple[list[date], list[float]] | None
    deflator_id: str | None
    starts: Mapping[str, list[int]]
    tp: traj_mod.TrajectoryParams


def _grid(dates: Sequence[date]) -> str:
    pairs = zip(dates, dates[1:], strict=False)
    gaps = sorted((b.year - a.year) * 12 + b.month - a.month for a, b in pairs)
    med = gaps[len(gaps) // 2] if gaps else 12
    return "M" if med <= 1 else "Q" if med <= 3 else "A"


def real_level(
    got: tuple[list[date], list[float]], index: tuple[list[date], list[float]], grid: str
) -> tuple[np.ndarray, int] | None:
    """Serie de valor dividida por el índice de precios (promedio de los meses del periodo).
    Devuelve (niveles reales, índice de grilla del primero); solo el tramo final sin huecos."""
    per = 12 // traj_mod.PER_YEAR[grid]
    idx: dict[int, float] = {d.year * 12 + d.month - 1: v for d, v in zip(*index, strict=True)}
    cells: dict[int, float] = {}
    for d, v in zip(*got, strict=True):
        m0 = (d.year * 12 + d.month - 1) // per * per
        px = [idx.get(m0 + k) for k in range(per)]
        if v > 0 and all(x is not None and x > 0 for x in px):
            cells[m0 // per] = v / (sum(px) / per)  # type: ignore[arg-type]
    if not cells:
        return None
    last = max(cells)
    first = last
    while first - 1 in cells:
        first -= 1
    return np.array([cells[i] for i in range(first, last + 1)]), first


def _trajectory_of(e: Mapping[str, Any], c: Ctx) -> tuple[str | None, str, str] | None:
    """(etiqueta, nivel, nota) de una serie de producción; None si no se puede evaluar."""
    key = (e["source"], e["object_id"])
    if e.get("measure_kind") == "cantidad":
        return e.get("trajectory"), "directo", ""
    got = c.series(*key)
    if not got or not got[0]:
        return None
    own = c.series_naics.get(key) or c.target
    hit = dm.price_source(own, c.prices) if own else None
    if hit is not None and c.series(*hit[1]):
        code, ids = hit
        index = c.series(*ids)
        level = "directo" if code == own else "indirecto"
        note = "" if level == "directo" else f"índice de precios de {code} para una serie de {own}"
    elif c.deflator and c.deflator[0]:
        index, level = c.deflator, "indirecto"
        note = f"sin índice de precios del productor: deflactor {c.deflator_id}"
    else:
        return None
    grid = _grid(got[0])
    real = real_level(got, index, grid)
    if real is None:
        return None
    levels, first = real
    label_, _ = traj_mod.classify(levels, first, grid, c.starts.get(grid, []), c.tp)
    return label_, level, note


def classify_quantity(evidence: Sequence[Mapping[str, Any]], c: Ctx) -> tuple[Out, Out]:
    """(cantidad, recuperacion): solo series de PRODUCCIÓN (ingresos, ventas, envíos). Las de
    valor se deflactan con el índice de precios del productor de su propia industria (✅), con el de
    otro nivel NAICS (🟡) o con el deflactor aprobado (🟡). Los insumos no cuentan."""
    qty = Out("cantidad", threshold="trayectoria acelera o gira_al_alza", unit="series")
    rec = Out("recuperacion", threshold="trayectoria frena_caida o rebote_bajo_pico", unit="series")
    pool, skipped, notes = [], 0, []
    for e in evidence:
        if e["item_kind"] != "series" or e.get("measure_subtype") == "poblacion":
            continue
        if e.get("measure_kind") not in ("cantidad", "valor"):
            continue
        if c.roles.get((e["source"], e["object_id"])) != "produccion":
            continue
        got = _trajectory_of(e, c)
        if got is None:
            skipped += 1
            continue
        pool.append({**e, "trajectory": got[0], "_level": got[1]})
        if got[2]:
            notes.append(got[2])
    if not pool:
        qty.note = "sin series de producción evaluables (los insumos no cuentan como cantidad)"
        rec.note = qty.note
        return qty, rec
    up = [e for e in pool if e["trajectory"] in COUNTS]
    back = [e for e in pool if e["trajectory"] in RECOVERY]
    for out, items in ((qty, up), (rec, back)):
        out.status = SI if items else NO
        out.value = float(len(items))
        out.source_ref, hidden = _refs(items)
        out.period = _period(items)
        shown = items or pool
        out.level = "directo" if any(e["_level"] == "directo" for e in shown) else "indirecto"
        extra = f" (+{hidden} con derechos, no listada(s))" if hidden else ""
        labels = ", ".join(sorted({str(e["trajectory"]) for e in pool}))
        out.note = (
            f"{len(items)} de {len(pool)} series de producción; trayectorias: {labels}"
            + (f"; {'; '.join(sorted(set(notes)))}" if notes else "")
            + (f"; {skipped} sin deflactor" if skipped else "")
            + extra
        )
    return qty, rec


def insumos_note(evidence: Sequence[Mapping[str, Any]], c: Ctx) -> str:
    """Insumos del fenómeno (empleo, nómina, gastos…) con su trayectoria, para productividad."""
    items = [
        e
        for e in evidence
        if e["item_kind"] == "series"
        and c.roles.get((e["source"], e["object_id"])) == "insumo"
        and e.get("measure_kind") in ("cantidad", "valor")
    ]
    pub = sorted(f"{_tag(e)} {e.get('trajectory') or 'sin etiqueta'}" for e in items if e["public"])
    hidden = sum(not e["public"] for e in items)
    if not items:
        return ""
    return "insumos: " + ", ".join(pub) + (f" (+{hidden} con derechos)" if hidden else "")


def _period(items: Sequence[Mapping[str, Any]]) -> str | None:
    ws = [e["window_start"] for e in items if e.get("window_start")]
    we = [e["window_end"] for e in items if e.get("window_end")]
    return f"{min(ws)} a {max(we)}" if ws and we else None


# --- precio ----------


def _cagr_pct(d0: date, v0: float, d1: date, v1: float) -> float | None:
    years = (d1 - d0).days / 365.25
    if years < 0.5 or v0 <= 0 or v1 <= 0:
        return None
    return 100.0 * (math.exp(math.log(v1 / v0) / years) - 1.0)


def classify_price(
    target: str | None,
    window_start: date | None,
    prices: Mapping[str, tuple[str, str]],
    series: dm.SeriesFn,
    deflator: tuple[list[date], list[float]] | None,
    deflator_id: str | None,
    p: ChangeParams,
    hidden_ids: frozenset[tuple[str, str]] = frozenset(),
) -> Out:
    out = Out(
        "precio", threshold=f"> {p.price_min_pp_year:g} pp/año sobre el deflactor", unit="pp/año"
    )
    if not target or not deflator or not deflator[0] or window_start is None:
        out.note = "sin industria, deflactor o ventana"
        return out
    hit = dm.price_source(target, prices)
    got = series(*hit[1]) if hit else None
    if hit is None or not got or not got[0]:
        out.note = "sin índice de precios del productor para la industria"
        return out
    code, (src, sid) = hit
    pa, da = _from(got, window_start), _from(deflator, window_start)
    if pa is None or da is None:
        out.note = "datos insuficientes en la ventana"
        return out
    g_p, g_d = _cagr_pct(*pa), _cagr_pct(*da)
    if g_p is None or g_d is None:
        out.note = "ventana demasiado corta"
        return out
    gap = g_p - g_d
    out.value = gap
    out.status = SI if gap > p.price_min_pp_year else NO
    out.period = f"{pa[0]:%Y-%m} a {pa[2]:%Y-%m}"
    out.source_ref = (
        "serie con derechos" if (src, sid) in hidden_ids else f"{src}:{sid} vs fred:{deflator_id}"
    )
    lvl = "" if code == target else f" (NAICS {code}, indirecto)"
    out.level = "directo" if code == target else "indirecto"
    out.note = f"precio {g_p:+.2f}%/año · deflactor {g_d:+.2f}%/año{lvl}"
    return out


def _from(s: tuple[list[date], list[float]], start: date) -> tuple[date, float, date, float] | None:
    pts = [(d, v) for d, v in zip(*s, strict=True) if d >= start and v]
    return (pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]) if len(pts) >= 2 else None


# --- productividad ----------


def _annual_deflator(deflator: tuple[list[date], list[float]] | None) -> dict[int, float]:
    acc: dict[int, list[float]] = defaultdict(list)
    for d, v in zip(*(deflator or ([], [])), strict=True):
        acc[d.year].append(v)
    return {y: sum(v) / len(v) for y, v in acc.items()}


def _pair(market: dm.Market, code: str, metric: str) -> dict[int, float]:
    return {y: v[0] for y, v in market.data.get((code, metric), {}).items()}


def classify_productivity(
    target: str | None,
    market: dm.Market,
    deflator: tuple[list[date], list[float]] | None,
    p: ChangeParams,
    defl_level: str = "indirecto",
    defl_note: str = "",
) -> Out:
    out = Out(
        "productividad",
        unit="% por año (ingreso real por trabajador)",
        threshold=(
            f"≥ {p.productivity_min_pct_year:g} %/año y empleo ≤ {p.employment_stable_pct_year:g} "
            "%/año"
        ),
    )
    defl = _annual_deflator(deflator)
    if not target or not defl:
        out.note = "sin industria o deflactor"
        return out
    for code in dm.ladder(target):
        rev = _pair(market, code, "revenue")
        emp = _pair(market, code, "employment_ec")
        est = _pair(market, code, "establishments_ec")
        years = sorted(y for y in set(rev) & set(emp) & set(defl) if emp[y] > 0 and rev[y] > 0)
        if len(years) < 2:
            continue
        y0, y1 = years[0], years[-1]
        n = y1 - y0
        real = {y: rev[y] / defl[y] for y in (y0, y1)}
        per_w = 100 * ((real[y1] / emp[y1]) / (real[y0] / emp[y0])) ** (1 / n) - 100
        per_e = None
        if est.get(y0, 0) > 0 and est.get(y1, 0) > 0:
            per_e = 100 * ((real[y1] / est[y1]) / (real[y0] / est[y0])) ** (1 / n) - 100
        # empleo de CBP en los mismos años si existe; si no, el del censo económico
        cbp = _pair(market, code, "employment")
        e0, e1, src = (
            (cbp[y0], cbp[y1], "employment")
            if cbp.get(y0, 0) > 0 and cbp.get(y1, 0) > 0
            else (emp[y0], emp[y1], "employment_ec")
        )
        g_emp = 100 * ((e1 / e0) ** (1 / n) - 1)
        rise = max(x for x in (per_w, per_e) if x is not None)
        out.value = per_w
        up = rise >= p.productivity_min_pct_year
        stable = g_emp <= p.employment_stable_pct_year
        out.status = SI if up and stable else NO
        out.period = f"{y0} a {y1}"
        out.source_ref = f"market/naics_metrics:revenue,{src}"
        pe = f" · por establecimiento {per_e:+.2f}%/año" if per_e is not None else ""
        lvl = "" if code == target else f" (NAICS {code}, indirecto)"
        out.level = "directo" if code == target and defl_level == "directo" else "indirecto"
        out.note = f"por trabajador {per_w:+.2f}%/año{pe} · empleo {g_emp:+.2f}%/año{lvl}" + (
            f" · deflactor: {defl_note}" if defl_note else ""
        )
        return out
    out.note = "sin ingresos y empleo en dos censos económicos"
    return out


# --- composición ----------


def _children(market: dm.Market, target: str, metric: str) -> dict[str, dict[int, float]]:
    n = len(target) + 1
    return {
        c: {y: v[0] for y, v in s.items()}
        for (c, m), s in market.data.items()
        if m == metric and len(c) == n and c.startswith(target) and "-" not in c
    }


def subindustry_shift(
    market: dm.Market, target: str, years_back: int
) -> tuple[float, int, int, str] | None:
    """(mayor cambio de peso en pp, año inicial, año final, subindustria) con el empleo de CBP de
    los hijos NAICS directos. Solo hijos presentes y > 0 en ambos años."""
    for metric in ("employment", "establishments"):
        kids = _children(market, target, metric)
        if len(kids) < 2:
            continue
        y1 = max(y for s in kids.values() for y in s)
        cand = sorted({y for s in kids.values() for y in s if y1 - years_back <= y < y1})
        for y0 in cand:
            both = {c: s for c, s in kids.items() if s.get(y0, 0) > 0 and s.get(y1, 0) > 0}
            if len(both) < 2:
                continue
            t0, t1 = sum(s[y0] for s in both.values()), sum(s[y1] for s in both.values())
            shifts = {c: 100 * (s[y1] / t1 - s[y0] / t0) for c, s in both.items()}
            c = max(shifts, key=lambda k: abs(shifts[k]))
            return shifts[c], y0, y1, c
    return None


def _delta(market: dm.Market, target: str, metric: str) -> tuple[float, int, int, str] | None:
    for code in dm.ladder(target):
        s = market.data.get((code, metric))
        if s and len(s) >= 2:
            y0, y1 = min(s), max(s)
            return s[y1][0] - s[y0][0], y0, y1, code
    return None


def classify_composition(target: str | None, market: dm.Market, p: ChangeParams) -> Out:
    out = Out(
        "composicion",
        unit="criterios cumplidos",
        threshold=(
            f"subindustrias ≥ {p.subindustry_shift_pp:g} pp; "
            f"CR4/CR8 ≥ {p.concentration_shift_pp:g} "
            f"pp; pequeñas ≥ {p.small_share_shift_pp:g} pp; entrada/salida ≥ "
            f"{p.entry_exit_gap_pp:g} pp"
        ),
    )
    if not target:
        out.note = "sin industria"
        return out
    parts: list[str] = []
    fired = 0
    seen = 0
    periods: list[str] = []

    def add(name: str, val: float, thr: float, period: str, extra: str = "") -> None:
        nonlocal fired, seen
        seen += 1
        hit = abs(val) >= thr
        fired += hit
        parts.append(f"{name} {val:+.1f} pp{extra} ({'sí' if hit else 'no'})")
        periods.append(period)

    if (sh := subindustry_shift(market, target, p.composition_years)) is not None:
        add("peso de subindustrias", sh[0], p.subindustry_shift_pp, f"{sh[1]} a {sh[2]}",
            f" [{sh[3]}]")  # fmt: skip
    for metric, name in (("cr4", "CR4"), ("cr8", "CR8")):
        if (d := _delta(market, target, metric)) is not None:
            add(name, d[0], p.concentration_shift_pp, f"{d[1]} a {d[2]}")
    if (d := _delta(market, target, "small_firm_share")) is not None:
        add("empresas pequeñas", d[0], p.small_share_shift_pp, f"{d[1]} a {d[2]}")
    for metric, name in (("entry_rate", "entrada"), ("exit_rate", "salida")):
        r = market.climb(target, metric)
        econ = market.latest("00", metric, r[1]) if r else None
        if r and econ:
            add(f"{name} vs economía total", r[2] - econ[1], p.entry_exit_gap_pp, str(r[1]))
    if not seen:
        out.note = "sin datos de composición para la industria"
        return out
    out.status = SI if fired else NO
    out.value = float(fired)
    out.period = "; ".join(sorted(set(periods)))
    out.source_ref = "market/naics_metrics"
    out.note = " · ".join(parts)
    return out


def classify_regulation() -> Out:
    return Out(
        "regulacion",
        note=NO_REGULATION_NOTE,
        threshold="sin vínculo documento-industria",
    )


# --- frase y construcción ----------


def phrase(rows: Sequence[Out]) -> str:
    by = {r.change_type: r for r in rows}
    changed = [label(t) for t in MAIN if by[t].status == SI]
    same = [label(t) for t in MAIN if by[t].status == NO]
    nodata = [
        f"{label(t)} ({by[t].note})" if t == "regulacion" else label(t)
        for t in MAIN
        if by[t].status == SD
    ]
    rec = by.get("recuperacion")
    back = ["cantidad"] if rec is not None and rec.status == SI else []

    def j(xs: Sequence[str]) -> str:
        return ", ".join(xs) if xs else "—"

    return (
        f"Cambió: {j(changed)} · No cambió: {j(same)} · Recuperación: {j(back)} · "
        f"Sin dato: {j(nodata)}"
    )


def build_change(
    phen: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    market: dm.Market,
    *,
    ctx: Ctx,
    p: ChangeParams,
    dp: dm.DimParams,
    hidden_ids: frozenset[tuple[str, str]] = frozenset(),
) -> list[Out]:
    target = dm.pick_naics(json.loads(phen["naics_json"]), dp.naics_min_weight_share)
    ctx = replace(ctx, target=target)
    qty, rec = classify_quantity(evidence, ctx)
    # Productividad: producción real = ingresos del censo / índice de precios del productor de la
    # industria (✅); con otro nivel NAICS o con el deflactor aprobado queda 🟡 y lo dice.
    hit = dm.price_source(target, ctx.prices) if target else None
    idx = ctx.series(*hit[1]) if hit else None
    if idx and idx[0]:
        defl, lvl = idx, "directo" if hit[0] == target else "indirecto"  # type: ignore[index]
        dnote = f"PCU {hit[0]}"  # type: ignore[index]
    else:
        defl, lvl, dnote = ctx.deflator, "indirecto", f"{ctx.deflator_id} (sin índice de precios)"
    prod = classify_productivity(target, market, defl, p, lvl, dnote)
    ins = insumos_note(evidence, ctx)
    if ins:
        prod.note = f"{prod.note} · {ins}" if prod.note else ins
    return [
        qty,
        classify_price(
            target,
            phen.get("window_start"),
            ctx.prices,
            ctx.series,
            ctx.deflator,
            ctx.deflator_id,
            p,
            hidden_ids,
        ),
        prod,
        classify_composition(target, market, p),
        classify_regulation(),
        rec,
    ]


def to_table_rows(run_id: str, phenomenon_id: str, rows: Sequence[Out]) -> list[dict[str, Any]]:
    text = phrase(rows)
    return [
        {
            "run_id": run_id,
            "phenomenon_id": phenomenon_id,
            "change_type": r.change_type,
            "status": r.status,
            "value": dm.clean(r.value),
            "unit": r.unit,
            "threshold": r.threshold,
            "period": r.period,
            "source_ref": r.source_ref,
            "note": r.note,
            "level": r.level or ("sin_dato" if r.status == SD else "directo"),
            "phrase": text,
        }
        for r in rows
    ]


def run_change(
    cfg: Mapping[str, Any], run_id: str | None = None, *, con: Any = None
) -> dict[str, Any]:
    """Calcula y escribe `rank/phenomenon_change` de una corrida (por defecto, la última)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from src.catalog import connect
    from src.detect.run import _resolve
    from src.ingest.store import read_table, upsert
    from src.rank import run as rank_run_mod
    from src.rank.run import rank_root
    from src.schemas import NAICS_LINKS, PHENOMENON_CHANGE, RANK_EVIDENCE, RANK_PHENOMENA

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
        from src.config import REPO_ROOT
        from src.rank import naics as naics_mod

        tables = naics_mod.load_tables(cfg, REPO_ROOT)
        mfile = root / "market" / "naics_metrics" / "part-0.parquet"
        market = dm.Market(pq.read_table(mfile).to_pylist() if mfile.exists() else [])
        pids = con.execute(
            "SELECT DISTINCT source, series_id FROM series_meta WHERE series_id LIKE 'PCU%'"
        ).fetchall()
        prices = dm.find_price_series(pids, tables)
        hidden = frozenset(
            (a, b)
            for a, b in con.execute(
                "SELECT source, series_id FROM series_meta WHERE series_id LIKE 'PCU%' "
                "AND copyrighted"
            ).fetchall()
        )
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
        p, dp = params_from(cfg), dm.params_from(cfg)
        tp = traj_mod.params_from(dict(cfg))
        months = rank_run_mod.recession_months(con, dict(cfg))
        starts = {g: traj_mod.recession_starts(months, g) for g in ("M", "Q", "A")}
        # rol de cada serie de evidencia (título y unidades oficiales) y su NAICS propio
        rules = role_mod.load_rules()
        keys = sorted({(e["source"], e["object_id"]) for e in ev if e["item_kind"] == "series"})
        meta = {
            (a, b): t or ""
            for a, b, t in con.execute(
                "SELECT source, series_id, title FROM series_meta"
            ).fetchall()
        }
        units = {
            (a, b): u or ""
            for a, b, u in con.execute(
                "SELECT source, series_id, any_value(units) FROM series GROUP BY 1, 2"
            ).fetchall()
        }
        roles = {
            k: role_mod.role_of(
                rules, source=k[0], series_id=k[1], units=units.get(k, ""), title=meta.get(k, "")
            )
            for k in keys
        }
        top: dict[tuple[str, str], tuple[float, str]] = {}
        for r in links:
            k = (r["source"], r["object_id"])
            if k not in top or r["peso"] > top[k][0]:
                top[k] = (r["peso"], r["naics"])
        series_naics = {
            k: (top[k][1] if k in top else (naics_mod.id_codes(k[0], k[1], tables) or [None])[0])
            for k in keys
        }
        ctx = Ctx(
            None, roles, series_naics, prices, series, deflator, ref.get("deflator"), starts, tp
        )
        by_cand: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in ev:
            by_cand[e["candidate_id"]].append(e)
        out: list[dict[str, Any]] = []
        for ph in sorted(phens, key=lambda r: r["phenomenon_id"]):
            evid = [e for c in json.loads(ph["cluster_ids_json"]) for e in by_cand.get(c, [])]
            rows = build_change(ph, evid, market, ctx=ctx, p=p, dp=dp, hidden_ids=hidden)
            out += to_table_rows(run_id, ph["phenomenon_id"], rows)
    finally:
        if own:
            con.close()
    table = pa.Table.from_pylist(out, schema=PHENOMENON_CHANGE.schema)
    upsert(
        table,
        PHENOMENON_CHANGE,
        rank_root(dict(cfg), "phenomenon_change"),
        source=part,
        n_buckets=rank_run_mod.N_BUCKETS,
        bucket_col="run_id",
    )
    dist: dict[str, dict[str, int]] = {t: {SI: 0, NO: 0, SD: 0} for t in (*MAIN, "recuperacion")}
    for r in out:
        dist[r["change_type"]][r["status"]] += 1
    return {
        "run_id": run_id,
        "phenomena": len(phens),
        "rows": len(out),
        "market_loaded": bool(market.data),
        "distribution": dist,
    }
