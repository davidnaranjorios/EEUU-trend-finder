"""Transformaciones descriptivas de la ficha del fenómeno (ADR 0009). Sin p ni q.

Todas son funciones puras sobre (fechas, valores):
- base 100, variación interanual y desestacionalización simple de NSA (medias por mes o
  trimestre del crecimiento, como `src.stats.transform`);
- real (deflactor) y per cápita (población), solo si las unidades son nominales según
  `context.nominal_units` / `not_nominal_units`;
- pendiente con varias ventanas, cambio por revisiones (vintages) y tramos de recesión.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from typing import Any

from src.report.narrative import annual_rate

# Escala numérica escrita en las unidades ("Millions of Dollars"): palabras genéricas de cantidad.
_SCALE = (("billion", 1e9), ("million", 1e6), ("thousand", 1e3))


def is_nominal(units: str | None, cfg: dict[str, Any]) -> bool:
    u = units or ""
    return bool(re.search(cfg["nominal_units"], u)) and not re.search(cfg["not_nominal_units"], u)


def unit_scale(units: str | None) -> float:
    u = (units or "").casefold()
    return next((f for word, f in _SCALE if word in u), 1.0)


def base100(dates: Sequence[date], values: Sequence[float], base: date) -> list[float] | None:
    ref = next((v for d, v in zip(dates, values, strict=True) if d >= base), None)
    if ref is None or ref == 0:
        return None
    return [v / ref * 100 for v in values]


def _period_key(d: date, freq: str) -> int:
    return d.month if freq in ("M", "W", "D") else (d.month - 1) // 3


def deseasonalize(dates: Sequence[date], values: Sequence[float], freq: str) -> list[float]:
    """Nivel sin estacionalidad: resta la media del crecimiento por mes (o trimestre)."""
    if freq not in ("M", "Q") or len(values) < 3 or any(v <= 0 for v in values):
        return list(values)
    logs = [math.log(v) for v in values]
    g = [b - a for a, b in zip(logs, logs[1:], strict=False)]
    by: dict[int, list[float]] = defaultdict(list)
    for d, x in zip(dates[1:], g, strict=True):
        by[_period_key(d, freq)].append(x)
    overall = sum(g) / len(g)
    means = {k: sum(v) / len(v) for k, v in by.items()}
    out, level = [values[0]], logs[0]
    for d, x in zip(dates[1:], g, strict=True):
        level += x - means[_period_key(d, freq)] + overall
        out.append(math.exp(level))
    return out


def yoy(dates: Sequence[date], values: Sequence[float]) -> list[tuple[date, float]]:
    """Variación interanual (%) de cada punto que tiene dato 12 meses antes."""
    idx = {(d.year, d.month): v for d, v in zip(dates, values, strict=True)}
    out = []
    for d, v in zip(dates, values, strict=True):
        prev = idx.get((d.year - 1, d.month))
        if prev and prev > 0 and v > 0:
            out.append((d, (v / prev - 1) * 100))
    return out


def monthly_lookup(dates: Sequence[date], values: Sequence[float]) -> dict[tuple[int, int], float]:
    return {(d.year, d.month): v for d, v in zip(dates, values, strict=True)}


def _ref_at(d: date, freq: str, ref: dict[tuple[int, int], float]) -> float | None:
    """Valor de la serie de referencia (mensual) en el periodo de `d` (promedio en Q y A)."""
    months = (
        [d.month]
        if freq in ("M", "W", "D")
        else ([d.month, d.month + 1, d.month + 2] if freq == "Q" else list(range(1, 13)))
    )
    vals = [ref[(d.year, m)] for m in months if (d.year, m) in ref]
    return sum(vals) / len(vals) if vals else None


def real(
    dates: Sequence[date],
    values: Sequence[float],
    freq: str,
    deflator: dict[tuple[int, int], float],
) -> tuple[list[date], list[float]] | None:
    """Valores en moneda del último periodo con deflactor (nivel × P_último / P_t)."""
    pairs = [(d, v, _ref_at(d, freq, deflator)) for d, v in zip(dates, values, strict=True)]
    pairs = [(d, v, p) for d, v, p in pairs if p]
    if len(pairs) < 3:
        return None
    last = pairs[-1][2]
    return [d for d, _, _ in pairs], [v * last / p for _, v, p in pairs]


def per_capita(
    dates: Sequence[date],
    values: Sequence[float],
    units: str,
    freq: str,
    population: dict[tuple[int, int], float],
    population_units: str,
) -> tuple[list[date], list[float]] | None:
    """Dólares por persona: valor × escala(unidades) / (población × escala(unidades_pob))."""
    s_val, s_pop = unit_scale(units), unit_scale(population_units)
    pairs = [(d, v, _ref_at(d, freq, population)) for d, v in zip(dates, values, strict=True)]
    pairs = [(d, v * s_val / (p * s_pop)) for d, v, p in pairs if p]
    if len(pairs) < 3:
        return None
    return [d for d, _ in pairs], [v for _, v in pairs]


def window_rates(
    dates: Sequence[date], values: Sequence[float], years: Sequence[int]
) -> dict[int, tuple[float, bool] | None]:
    if not dates:
        return dict.fromkeys(years)
    end = dates[-1]
    return {
        y: annual_rate(dates, values, date(end.year - y, end.month, min(end.day, 28)), end)
        for y in years
    }


def revision_change(
    vintage_rows: Sequence[tuple[date, float, date]],
    current: tuple[Sequence[date], Sequence[float]],
    window: tuple[date, date],
) -> dict[str, Any] | None:
    """Pendiente de la ventana con los datos del primer vintage que la cubre vs la actual."""
    covering = sorted({v for d, _, v in vintage_rows if v >= window[1]})
    if not covering:
        return None
    first = covering[0]
    latest: dict[date, tuple[date, float]] = {}
    for d, val, v in vintage_rows:
        if v <= first and (d not in latest or v > latest[d][0]):
            latest[d] = (v, val)
    ds = sorted(latest)
    then = annual_rate(ds, [latest[d][1] for d in ds], *window)
    now = annual_rate(current[0], current[1], *window)
    if then is None or now is None:
        return None
    return {"vintage": first, "then": then, "now": now}


def recession_bands(dates: Sequence[date], values: Sequence[float]) -> list[tuple[date, date]]:
    bands, start = [], None
    for d, v in zip(dates, values, strict=True):
        if v >= 0.5 and start is None:
            start = d
        elif v < 0.5 and start is not None:
            bands.append((start, d))
            start = None
    if start is not None:
        bands.append((start, dates[-1]))
    return bands


def _fmt_rate(r: tuple[float, bool] | None) -> str | None:
    if not r:
        return None
    value, pct = r
    return f"{value:+.1f} %" if pct else f"{value:+.3g}/año"


def build_phenomenon(
    series: Sequence[dict[str, Any]],
    values: dict[tuple[str, str], tuple[list[date], list[float]]],
    payload: dict[str, Any] | None,
    related_values: dict[tuple[str, str], tuple[list[date], list[float]]],
    related_meta: dict[tuple[str, str], dict[str, Any]],
    refs: dict[str, dict[str, Any]],
    vintages: dict[tuple[str, str], list[tuple[date, float, date]]],
    cfg: dict[str, Any],
    forbidden: set[tuple[str, str]],
) -> dict[str, Any]:
    """Ficha descriptiva de un candidato: núcleo en base 100, transformaciones, relacionadas,
    dinámica y robustez. Solo series públicas (las de `series` ya vienen filtradas)."""
    from src.report.charts import line_chart, multi_line_chart

    rec = refs.get("recessions", {})
    bands = recession_bands(rec.get("dates") or [], rec.get("values") or [])
    starts = [s["window"][0] for s in series if s["window"][0]]
    base = min(starts) if starts else None
    lines, transforms, robust = [], [], []
    defl = monthly_lookup(refs["deflator"]["dates"], refs["deflator"]["values"])
    pop = monthly_lookup(refs["population"]["dates"], refs["population"]["values"])
    for s in series:
        key = (s["source"], s["id"])
        d, v = values.get(key, ([], []))
        if not d:
            continue
        sa = (s["seasonal_adjustment"] or "").startswith("NSA")
        level = deseasonalize(d, v, s["frequency"]) if sa else list(v)
        if base:
            b100 = base100(d, level, base)
            if b100:
                lines.append((s["title"] + (" (desestacionalizada)" if sa else ""), d, b100))
        y = yoy(d, v)
        nominal = is_nominal(s["units"], cfg)
        row = {
            "title": s["title"],
            "units": s["units"],
            "last": v[-1],
            "last_date": d[-1],
            "yoy": f"{y[-1][1]:+.1f} %" if y else None,
            "annual": _fmt_rate(annual_rate(d, v, date(d[-1].year - 5, d[-1].month, 1), d[-1])),
            "nominal": nominal,
            "seasonally_adjusted_here": sa,
            "real_annual": None,
            "per_capita": None,
        }
        if nominal and defl:
            rv = real(d, v, s["frequency"], defl)
            if rv:
                row["real_annual"] = _fmt_rate(
                    annual_rate(
                        rv[0], rv[1], date(rv[0][-1].year - 5, rv[0][-1].month, 1), rv[0][-1]
                    )
                )
        if nominal and pop:
            pc = per_capita(d, v, s["units"], s["frequency"], pop, refs["population"]["units"])
            if pc:
                row["per_capita"] = pc[1][-1]
        transforms.append(row)
        rates = window_rates(d, v, cfg["windows_years"])
        rev = (
            revision_change(vintages.get(key, []), (d, v), tuple(s["window"]))
            if s["window"][0]
            else None
        )
        robust.append(
            {
                "title": s["title"],
                "windows": {y_: _fmt_rate(r) for y_, r in rates.items()},
                "revision": None
                if rev is None
                else {
                    "vintage": rev["vintage"],
                    "then": _fmt_rate(rev["then"]),
                    "now": _fmt_rate(rev["now"]),
                },
            }
        )
    related = [
        r
        for r in (payload or {}).get("related", [])
        if (r["source"], r["series_id"]) not in forbidden
    ]
    missing_refs = [f"{role} ({r['id']})" for role, r in refs.items() if not r["dates"]]
    stack = stacked_panel(series, values, related, related_values, related_meta, bands, line_chart)
    changes = [s.get("pelt") or s["window"][0] for s in series if s["window"][0]]
    return {
        "chart": multi_line_chart(lines, label="Series del cluster en base 100", bands=bands)
        if lines
        else None,
        "base": base,
        "change_date": sorted(changes)[len(changes) // 2] if changes else None,
        "transforms": transforms,
        "related": related,
        "robust": robust,
        "has_recessions": bool(bands),
        "stack": stack,
        "missing_refs": missing_refs,
        "n_pool": (payload or {}).get("n_pool", 0),
    }


def stacked_panel(
    series: Sequence[dict[str, Any]],
    values: dict[tuple[str, str], tuple[list[date], list[float]]],
    related: Sequence[dict[str, Any]],
    related_values: dict[tuple[str, str], tuple[list[date], list[float]]],
    related_meta: dict[tuple[str, str], dict[str, Any]],
    bands: Sequence[tuple[date, date]],
    line_chart: Any,
) -> list[dict[str, Any]]:
    """Gráficos apilados con el mismo eje de fechas: núcleo primero, después las relacionadas
    descargadas, cada una en sus unidades y con las recesiones sombreadas."""
    entries = []
    core = next((s for s in series if values.get((s["source"], s["id"]), ([], []))[0]), None)
    if core:
        entries.append(
            {
                "key": f"{core['source']}|{core['id']}",
                "title": core["title"],
                "units": core["units"],
                "source": core["source"],
                "id": core["id"],
                "role": "núcleo",
                "core": True,
                "data": values[(core["source"], core["id"])],
            }
        )
    for r in related:
        k = (r["source"], r["series_id"])
        if not r["downloaded"] or k not in related_values or not related_values[k][0]:
            continue
        m = related_meta.get(k, {})
        entries.append(
            {
                "key": f"{k[0]}|{k[1]}",
                "title": m.get("title") or r["title"],
                "units": m.get("units") or r.get("units") or "",
                "source": k[0],
                "id": k[1],
                "role": r["role"],
                "core": False,
                "data": related_values[k],
            }
        )
    if not entries:
        return []
    start = min(min(e["data"][0]) for e in entries)  # eje común: unión de los tramos
    end = max(max(e["data"][0]) for e in entries)
    out = []
    for e in entries:
        d, v = e.pop("data")
        e["chart"] = line_chart(
            d, v, label=f"{e['title']} ({e['units']})", bands=bands, x_range=(start, end)
        )
        out.append(e)
    return out
