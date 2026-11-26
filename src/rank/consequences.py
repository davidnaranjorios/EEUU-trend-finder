"""Capa 3, pregunta 2: ¿qué consecuencias genera? (ADR 0032). Reglas fijas, sin IA.

Por fenómeno (candidato o en observación) y con la industria NAICS de las dimensiones, seis
consecuencias, cada cifra con valor, unidad, periodo, fuente y nivel (✅ directo / 🟡 indirecto /
⚪ sin dato):

- compradores: participación de cada destino en el uso total del producto de la industria
  (demanda final por categoría y las industrias compradoras principales), de `market/io_flows`;
- proveedores: las industrias proveedoras principales y su participación en los insumos;
- escasez de capacidad: producción real por trabajador (de «qué cambió»), nómina por trabajador
  frente a la economía y vacantes de una serie ingerida del mismo NAICS;
- pérdida de participación: tasa de salida frente a la economía y cambio de CR4 entre censos;
- márgenes: ingresos menos gastos si ambas series existen para el mismo NAICS;
- problemas declarados: términos de `item_1a` sobrerrepresentados en las empresas del NAICS.

Solo describe: no filtra ni toca detectores, FDR, ranking, dimensiones ni «qué cambió». Los
umbrales viven en `config.yaml` (`rank.consequences`); la industria, las series y los términos
salen de los datos, y los códigos BEA y NAICS de las concordancias oficiales.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src.rank import dimensions as dm
from src.rank.change import SD as CHANGE_SD

DIRECT, INDIRECT, NONE = "directo", "indirecto", "sin_dato"
IO_REF = "market/io_flows"


@dataclass(frozen=True)
class ConsParams:
    top_n: int = 5
    vacancies_regex: str = r"(?i)job openings"
    revenue_regex: str = r"(?i)^total revenue for (?P<rest>.+)$"
    expenses_regex: str = r"(?i)^total (?:operating )?expenses for (?P<rest>.+)$"
    min_overrepresentation: float = 2.0
    min_companies: int = 5
    min_docs_naics: int = 3
    max_items: int = 10
    payroll_multiplier: float = 1000.0  # PAYANN de CBP viene en miles de dólares


def params_from(cfg: Mapping[str, Any]) -> ConsParams:
    c = cfg["rank"]["consequences"]
    return ConsParams(
        int(c["top_n"]),
        str(c["vacancies_regex"]),
        str(c["revenue_regex"]),
        str(c["expenses_regex"]),
        float(c["min_overrepresentation"]),
        int(c["min_companies"]),
        int(c["min_docs_naics"]),
        int(c["max_items"]),
        float(c["payroll_multiplier"]),
    )


@dataclass
class Row:
    consequence: str
    metric: str
    value: float | None = None
    value_text: str | None = None
    unit: str | None = None
    period: str | None = None
    source_ref: str | None = None
    level: str = NONE
    naics_used: str | None = None
    public: bool = True  # False: la serie de origen tiene copyright (no se muestra su id)
    note: str | None = None


def missing(consequence: str, metric: str, target: str | None, note: str | None = None) -> Row:
    return Row(consequence, metric, naics_used=target, note=note)


def _level(target: str, used: str) -> str:
    return DIRECT if used == target else INDIRECT


# --- insumo-producto ----------------------------------------------------------------------------


class IoFlows:
    """`market/io_flows` en memoria con índices por año y por código BEA."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = list(rows)
        self.by_from: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
        self.by_to: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
        self.naics_of: dict[str, set[str]] = defaultdict(set)
        self.categories: set[str] = set()
        for r in self.rows:
            self.by_from[(r["from_industry"], int(r["year"]))].append(r)
            self.by_to[(r["to_industry"], int(r["year"]))].append(r)
            if r["from_kind"] == "industria" and r.get("from_naics"):
                self.naics_of[r["from_industry"]].update(r["from_naics"].split(";"))
            if r.get("demand_category"):
                self.categories.add(r["demand_category"])

    def codes_for(self, target: str) -> tuple[list[str], str] | None:
        """Códigos BEA cuyo NAICS (concordancia oficial) es el del objetivo o su ancestro más
        cercano, y el código NAICS usado. Entre varios, los de menos NAICS (más específicos)."""
        for code in dm.ladder(target):
            hits = [b for b, ns in self.naics_of.items() if code in ns]
            if hits:
                narrow = min(len(self.naics_of[b]) for b in hits)
                return sorted(b for b in hits if len(self.naics_of[b]) == narrow), code
        return None

    def latest_year(self, code: str, side: str) -> int | None:
        idx = self.by_from if side == "from" else self.by_to
        years = [y for (c, y) in idx if c == code]
        return max(years) if years else None


def _bea_rows(target: str | None, io: IoFlows | None, p: ConsParams) -> tuple[list[Row], list[Row]]:
    """(compradores, proveedores) de la tabla de uso, o sus filas ⚪."""
    c_names = [f"demanda_{k}" for k in sorted(io.categories)] if io else []
    buyers_none = [
        missing("compradores", m, target, "sin tabla de insumo-producto para la industria")
        for m in ("demanda_final", "comprador_1")
    ]
    sellers_none = [
        missing(
            "proveedores", "proveedor_1", target, "sin tabla de insumo-producto para la industria"
        )
    ]
    if not target or io is None:
        return buyers_none, sellers_none
    hit = io.codes_for(target)
    if hit is None:
        note = f"el NAICS {target} no tiene código BEA en la concordancia oficial"
        return (
            [missing("compradores", "demanda_final", target, note)],
            [missing("proveedores", "proveedor_1", target, note)],
        )
    codes, used = hit
    level = _level(target, used)
    code = codes[0]
    extra = f" (BEA {'+'.join(codes)})" if len(codes) > 1 else f" (BEA {code})"
    note = None if level == DIRECT else f"industria BEA{extra} contiene a {target}"

    out_b: list[Row] = []
    year = io.latest_year(code, "from")
    cells = io.by_from.get((code, year), []) if year else []
    totals = [r["value"] for r in cells if r["to_kind"] == "total"]
    if not cells or not totals or max(totals) <= 0:
        out_b.append(missing("compradores", "demanda_final", target, "sin datos de uso"))
    else:
        total = max(totals)  # el mayor de los totales de la fila es el uso total del producto
        ref = f"{IO_REF}:{code}"
        per: dict[str, float] = defaultdict(float)
        for r in cells:
            if r["to_kind"] == "demanda_final" and r.get("demand_category"):
                per[r["demand_category"]] += r["value"]
        for i, cat in enumerate(sorted(io.categories)):
            out_b.append(
                Row(
                    "compradores",
                    c_names[i],
                    100.0 * per.get(cat, 0.0) / total,
                    f"{cat}: {per.get(cat, 0.0):,.0f} de {total:,.0f} millones",
                    "% del uso total del producto",
                    str(year),
                    ref,
                    level,
                    used,
                    note=note,
                )
            )
        ind = sorted(
            (r for r in cells if r["to_kind"] == "industria" and r["value"] > 0),
            key=lambda r: (-r["value"], r["to_industry"]),
        )[: p.top_n]
        # Dinero (ADR 0036): lo que los compradores pagan por el producto, en dólares corrientes.
        out_b.append(
            Row(
                "compradores",
                "gasto_total",
                total,
                None,
                "millones de dólares corrientes",
                str(year),
                ref,
                level,
                used,
                note=note,
            )
        )
        for i, r in enumerate(ind, 1):
            out_b.append(
                Row(
                    "compradores",
                    f"gasto_comprador_{i}",
                    r["value"],
                    f"{r['to_desc'] or r['to_industry']} (BEA {r['to_industry']})",
                    "millones de dólares corrientes",
                    str(year),
                    ref,
                    level,
                    used,
                    note=note,
                )
            )
        for i, r in enumerate(ind, 1):
            out_b.append(
                Row(
                    "compradores",
                    f"comprador_{i}",
                    100.0 * r["value"] / total,
                    f"{r['to_desc'] or r['to_industry']} (BEA {r['to_industry']})",
                    "% del uso total del producto",
                    str(year),
                    ref,
                    level,
                    used,
                    note=note,
                )
            )
        if not ind:
            out_b.append(
                missing("compradores", "comprador_1", target, "sin industrias compradoras")
            )

    out_s: list[Row] = []
    y2 = io.latest_year(code, "to")
    inputs = [
        r for r in (io.by_to.get((code, y2), []) if y2 else []) if r["from_kind"] == "industria"
    ]
    base = sum(r["value"] for r in inputs if r["value"] > 0)
    if not inputs or base <= 0:
        out_s.append(
            missing("proveedores", "proveedor_1", target, "sin insumos de otras industrias")
        )
    else:
        top = sorted(
            (r for r in inputs if r["value"] > 0), key=lambda r: (-r["value"], r["from_industry"])
        )[: p.top_n]
        for i, r in enumerate(top, 1):
            out_s.append(
                Row(
                    "proveedores",
                    f"proveedor_{i}",
                    100.0 * r["value"] / base,
                    f"{r['from_desc'] or r['from_industry']} (BEA {r['from_industry']})",
                    "% de los insumos de otras industrias",
                    str(y2),
                    f"{IO_REF}:{code}",
                    level,
                    used,
                    note=note,
                )
            )
    return out_b, out_s


# --- escasez, participación y márgenes (cifras de mercado y series) -----------------------------


def _payroll_rows(target: str | None, market: dm.Market, mult: float) -> list[Row]:
    names = ("nomina_por_trabajador", "nomina_vs_economia")
    if not target:
        return [missing("escasez", n, target) for n in names]
    for code in dm.ladder(target):
        pay, emp = market.data.get((code, "payroll")), market.data.get((code, "employment"))
        years = sorted(set(pay or {}) & set(emp or {}), reverse=True)
        for y in years:
            econ_p, econ_e = market.latest("00", "payroll", y), market.latest("00", "employment", y)
            if emp[y][0] <= 0 or not econ_p or not econ_e or econ_e[1] <= 0:
                continue
            mine = pay[y][0] * mult / emp[y][0]
            theirs = econ_p[1] * mult / econ_e[1]
            ref = f"{IO_REF.replace('io_flows', 'naics_metrics')}:payroll,employment"
            level = _level(target, code)
            return [
                Row(
                    "escasez",
                    names[0],
                    mine,
                    f"economía: {theirs:,.0f} dólares",
                    "dólares por trabajador al año",
                    str(y),
                    ref,
                    level,
                    code,
                ),
                Row(
                    "escasez",
                    names[1],
                    mine / theirs,
                    f"{mine:,.0f} frente a {theirs:,.0f} dólares",
                    "veces el promedio de la economía",
                    str(y),
                    ref,
                    level,
                    code,
                ),
            ]
    return [missing("escasez", n, target, "sin nómina y empleo del mismo NAICS") for n in names]


def _exit_rows(target: str | None, market: dm.Market) -> list[Row]:
    out: list[Row] = []
    hit = market.climb(target, "exit_rate") if target else None
    econ = market.latest("00", "exit_rate", hit[1]) if hit else None
    if hit and econ and econ[1] > 0:
        code, year, rate, unit, _ = hit
        out.append(
            Row(
                "perdida_participacion",
                "salida_vs_economia",
                rate / econ[1],
                f"{rate:.3f} % frente a {econ[1]:.3f} % de la economía ({rate - econ[1]:+.3f} pp)",
                "veces la tasa de salida de la economía",
                str(year),
                "market/naics_metrics:exit_rate",
                _level(target or code, code),
                code,
            )
        )
    else:
        out.append(missing("perdida_participacion", "salida_vs_economia", target))
    d = None
    for code in dm.ladder(target) if target else []:
        s = market.data.get((code, "cr4"))
        if s and len(s) >= 2:
            y0, y1 = min(s), max(s)
            d = (s[y1][0] - s[y0][0], y0, y1, code, s[y0][0], s[y1][0])
            break
    if d:
        out.append(
            Row(
                "perdida_participacion",
                "cambio_cr4",
                d[0],
                f"{d[4]:.1f} % en {d[1]} a {d[5]:.1f} % en {d[2]}",
                "puntos porcentuales",
                f"{d[1]} a {d[2]}",
                "market/naics_metrics:cr4",
                _level(target or d[3], d[3]),
                d[3],
            )
        )
    else:
        out.append(missing("perdida_participacion", "cambio_cr4", target))
    return out


@dataclass(frozen=True)
class SeriesRef:
    source: str
    series_id: str
    title: str
    public: bool = True


SeriesFn = Callable[[str, str], "tuple[list[date], list[float]] | None"]


def _vacancy_row(
    target: str | None,
    by_naics: Mapping[str, Sequence[SeriesRef]],
    series: SeriesFn,
    p: ConsParams,
) -> Row:
    name = "vacantes"
    if not target:
        return missing("escasez", name, target)
    rx = re.compile(p.vacancies_regex)
    for code in dm.ladder(target):
        for ref in sorted(by_naics.get(code, ()), key=lambda r: (r.source, r.series_id)):
            got = series(ref.source, ref.series_id)
            if rx.search(ref.title) and got and got[0]:
                d, v = got[0][-1], got[1][-1]
                return Row(
                    "escasez",
                    name,
                    float(v),
                    ref.title if ref.public else None,
                    "según la serie (vacantes)",
                    f"{d:%Y-%m}",
                    f"{ref.source}:{ref.series_id}",
                    _level(target, code),
                    code,
                    public=ref.public,
                )
    return missing("escasez", name, target, "sin serie de vacantes (JOLTS) ingerida para el NAICS")


def _margin_row(
    target: str | None,
    by_naics: Mapping[str, Sequence[SeriesRef]],
    series: SeriesFn,
    p: ConsParams,
) -> Row:
    name = "ingresos_menos_gastos"
    if not target:
        return missing("margenes", name, target)
    rev_re, exp_re = re.compile(p.revenue_regex), re.compile(p.expenses_regex)
    for code in dm.ladder(target):
        revs: dict[str, SeriesRef] = {}
        exps: dict[str, SeriesRef] = {}
        for ref in sorted(by_naics.get(code, ()), key=lambda r: (r.source, r.series_id)):
            if m := rev_re.search(ref.title):
                revs.setdefault(m["rest"].strip().lower(), ref)
            if m := exp_re.search(ref.title):
                exps.setdefault(m["rest"].strip().lower(), ref)
        for rest in sorted(set(revs) & set(exps)):
            a, b = (
                series(revs[rest].source, revs[rest].series_id),
                series(exps[rest].source, exps[rest].series_id),
            )
            if not a or not b:
                continue
            common = sorted(set(a[0]) & set(b[0]))
            if not common:
                continue
            d = common[-1]
            rv, ex = a[1][a[0].index(d)], b[1][b[0].index(d)]
            if not rv:
                continue
            pub = revs[rest].public and exps[rest].public
            return Row(
                "margenes",
                name,
                float(rv - ex),
                f"ingresos {rv:,.0f} − gastos {ex:,.0f} "
                f"({100 * (rv - ex) / rv:+.1f} % de los ingresos)",
                "misma unidad de las series",
                f"{d:%Y-%m}",
                f"{revs[rest].source}:{revs[rest].series_id} − "
                f"{exps[rest].source}:{exps[rest].series_id}",
                _level(target, code),
                code,
                public=pub,
            )
    return missing("margenes", name, target, "sin series de ingresos y de gastos del mismo NAICS")


def _productivity_row(
    change_rows: Mapping[str, Mapping[str, Any]] | None, target: str | None
) -> Row:
    name = "produccion_real_por_trabajador"
    r = (change_rows or {}).get("productividad")
    if not r or r["status"] == CHANGE_SD or r.get("value") is None:
        return missing("escasez", name, target, "sin dato de productividad en «qué cambió»")
    return Row(
        "escasez",
        name,
        float(r["value"]),
        r.get("note"),
        r.get("unit"),
        r.get("period"),
        r.get("source_ref"),
        r["level"] if r["level"] in (DIRECT, INDIRECT) else INDIRECT,
        target,
    )


# --- problemas declarados (sobrerrepresentación) ------------------------------------------------


@dataclass(frozen=True)
class TermCounts:
    doc_count: int  # documentos que mencionan el término en su ventana
    total_docs: int  # documentos de la sección en esos periodos
    sic_docs: Mapping[str, int]  # {sic: documentos con el término} (solo los SIC más frecuentes)
    window: str  # ventana de la señal («2022Q1..2024Q4»)


Counts = Callable[[str], "tuple[Mapping[str, int], Mapping[str, int]]"]


@dataclass
class TextCtx:
    """Conteos de texto (todo sale de `term_freq` y `text_structure`; la concordancia SIC → NAICS
    es la oficial de Census). `counts(ventana)` da, de todos los documentos de EDGAR en la
    ventana, los documentos y las empresas distintas por SIC de 4 dígitos."""

    sic_naics: Mapping[str, Sequence[tuple[str, float]]]
    counts: Counts
    terms: Mapping[str, TermCounts] = field(default_factory=dict)


def sic_weight(ctx: TextCtx, sic: str, code: str) -> float:
    return sum(w for naics, w in ctx.sic_naics.get(sic, ()) if naics.startswith(code))


@dataclass(frozen=True)
class Over:
    term: str
    ratio: float
    pct_naics: float
    pct_all: float
    docs: float
    companies: int
    window: str


def overrepresented(code: str, ctx: TextCtx, p: ConsParams) -> tuple[list[Over], bool]:
    """Términos cuya proporción de documentos en el NAICS supera a la de todos los documentos.

    Sobrerrepresentación = (documentos del NAICS con el término ÷ documentos del NAICS) ÷
    (documentos con el término ÷ documentos totales), ambas en la ventana de la señal. Un NAICS
    se evalúa solo con ≥ `min_companies` empresas en la ventana y un término cuenta con ≥
    `min_docs_naics` documentos del NAICS; solo se listan los de razón ≥ `min_overrepresentation`.
    El segundo valor dice si el NAICS tuvo empresas suficientes en alguna ventana."""
    cache: dict[str, tuple[float, float]] = {}

    def base(window: str) -> tuple[float, float]:
        if window not in cache:
            docs, comps = ctx.counts(window)
            cache[window] = (
                sum(w * n for s, n in docs.items() if (w := sic_weight(ctx, s, code))),
                sum(w * n for s, n in comps.items() if (w := sic_weight(ctx, s, code))),
            )
        return cache[window]

    out: list[Over] = []
    evaluable = False
    for term, tc in sorted(ctx.terms.items()):
        docs_n, comp_n = base(tc.window)
        if comp_n < p.min_companies or docs_n <= 0:
            continue
        evaluable = True
        c_n = sum(w * n for s, n in tc.sic_docs.items() if (w := sic_weight(ctx, s, code)))
        if c_n < p.min_docs_naics or tc.doc_count <= 0 or tc.total_docs <= 0:
            continue
        mine, rest = c_n / docs_n, tc.doc_count / tc.total_docs
        ratio = mine / rest
        if ratio >= p.min_overrepresentation:
            out.append(
                Over(term, ratio, 100 * mine, 100 * rest, c_n, int(round(comp_n)), tc.window)
            )
    out.sort(key=lambda o: (-o.ratio, -o.docs, o.term))
    return out[: p.max_items], evaluable


def _problem_rows(target: str | None, ctx: TextCtx | None, p: ConsParams) -> list[Row]:
    name = "terminos_propios"
    if not target:
        return [missing("problemas", name, target)]
    if ctx is None or not ctx.terms:
        return [missing("problemas", name, target, "sin datos de texto con industria")]
    for code in dm.ladder(target):
        found, evaluable = overrepresented(code, ctx, p)
        if not evaluable:
            continue  # pocas empresas en este nivel: se sube de nivel
        level = _level(target, code)
        if not found:
            return [missing("problemas", name, code, "sin riesgos propios de la industria")]
        return [
            Row(
                "problemas",
                f"termino_{i}",
                o.ratio,
                f"{o.term}: {o.pct_naics:.1f} % de los reportes del NAICS frente a "
                f"{o.pct_all:.1f} % de todos ({o.docs:.0f} documentos, {o.companies} empresas)",
                "veces la proporción general",
                o.window,
                f"edgar:item_1a|{o.term}",
                level,
                code,
            )
            for i, o in enumerate(found, 1)
        ]
    return [missing("problemas", name, target, f"menos de {p.min_companies} empresas en el NAICS")]


# --- ensamblado ---------------------------------------------------------------------------------


def build_consequences(
    phen: Mapping[str, Any],
    market: dm.Market,
    *,
    io: IoFlows | None,
    series: SeriesFn,
    by_naics: Mapping[str, Sequence[SeriesRef]],
    change_rows: Mapping[str, Mapping[str, Any]] | None,
    text: TextCtx | None,
    dp: dm.DimParams,
    p: ConsParams,
) -> list[Row]:
    target = dm.pick_naics(json.loads(phen["naics_json"]), dp.naics_min_weight_share)
    buyers, providers = _bea_rows(target, io, p)
    rows = [
        *buyers,
        *providers,
        _productivity_row(change_rows, target),
        *_payroll_rows(target, market, p.payroll_multiplier),
        _vacancy_row(target, by_naics, series, p),
        *_exit_rows(target, market),
        _margin_row(target, by_naics, series, p),
        *_problem_rows(target, text, p),
    ]
    return rows


def to_table_rows(run_id: str, phenomenon_id: str, rows: Sequence[Row]) -> list[dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    pos: dict[str, int] = defaultdict(int)
    for r in rows:
        pos[r.consequence] += 1
        out[(r.consequence, r.metric)] = {
            "run_id": run_id,
            "phenomenon_id": phenomenon_id,
            "consequence": r.consequence,
            "metric": r.metric,
            "value": dm.clean(r.value),
            "value_text": r.value_text,
            "unit": r.unit,
            "period": r.period,
            "source_ref": r.source_ref,
            "level": r.level,
            "naics_used": r.naics_used,
            "public": r.public,
            "note": r.note,
            "position": pos[r.consequence],
        }
    return list(out.values())


def headline(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """Línea de portada: comprador principal y consecuencia de mayor magnitud con dato.

    Comprador principal: el destino con mayor participación en el uso del producto (una categoría
    de demanda final o una industria). Mayor magnitud: entre las cifras que son «veces el
    promedio» (nómina por trabajador y tasa de salida frente a la economía, y la
    sobrerrepresentación de términos), la que más se aleja de 1 (|x − 1|); sin umbrales."""
    buyers = [
        r
        for r in rows
        if r["consequence"] == "compradores"
        and r["value"] is not None
        and r["level"] != NONE
        and not r["metric"].startswith("gasto_")  # en dólares: Dinero, no participación
    ]
    if not buyers:
        return None
    b = max(buyers, key=lambda r: (r["value"], r["metric"]))
    who = (
        b["metric"].removeprefix("demanda_")
        if b["metric"].startswith("demanda_")
        else (b["value_text"] or b["metric"])
    )
    parts = [f"comprador principal: {who} ({b['value']:.1f} % del uso)"]
    names = {
        "nomina_vs_economia": "nómina por trabajador",
        "salida_vs_economia": "tasa de salida de empresas",
    }
    cands: list[tuple[float, str]] = []
    for r in rows:
        if r["value"] is None or r["level"] == NONE:
            continue
        if r["metric"] in names:
            cands.append(
                (abs(r["value"] - 1.0), f"{names[r['metric']]} {r['value']:.2f}× la economía")
            )
        elif r["metric"].startswith("termino_"):
            term = (r["value_text"] or "").split(":")[0]
            cands.append((abs(r["value"] - 1.0), f"término propio «{term}» {r['value']:.1f}×"))
    if cands:
        parts.append("mayor consecuencia: " + max(cands)[1])
    return " · ".join(parts)


# --- carga y escritura --------------------------------------------------------------------------


def _text_ctx(
    cfg: Mapping[str, Any], con: Any, run_row: Mapping[str, Any] | None, tables: Any
) -> TextCtx | None:
    """Términos con señal en la sección de riesgos (q ≤ `fdr.q`) de la corrida de texto de EDGAR,
    sus conteos en la ventana de la señal y los documentos por SIC de `text_structure`."""
    from src.rank import run as rank_run_mod

    if not run_row:
        return None
    text_runs = json.loads(run_row.get("text_run_ids_json") or "{}")
    rid = text_runs.get("edgar")
    if not rid:
        return None
    sec = re.compile(cfg["rank"]["dimensions"]["risk_section_regex"])
    q = float(cfg["fdr"]["q"])
    sigs = [
        s
        for s in rank_run_mod._signals(dict(cfg), "edgar", rid)
        if sec.search(str(s["object_id"]).split("|", 1)[0])
        and s["q_value"] is not None
        and s["q_value"] <= q
    ]
    if not sigs:
        return None
    try:
        import pyarrow as pa

        con.register(
            "_cterms",
            pa.table(
                {
                    "section": [s["object_id"].split("|", 1)[0] for s in sigs],
                    "term": [rank_run_mod.term_text(s["object_id"]) for s in sigs],
                }
            ),
        )
        rows = con.execute(
            "SELECT f.section, f.term, f.period, f.doc_count, f.total_docs, f.sic_top_json "
            "FROM term_freq f JOIN _cterms t USING (section, term) WHERE f.source = 'edgar' "
            "AND f.sic_top_json IS NOT NULL"
        ).fetchall()
    except Exception:  # sin term_freq o sin sic_top_json: la consecuencia queda ⚪
        return None
    from src.rank import naics as naics_mod

    win = {
        (s["object_id"].split("|", 1)[0], rank_run_mod.term_text(s["object_id"])): s for s in sigs
    }
    acc: dict[str, dict[str, Any]] = {}
    for section, term, period, c, n, payload in rows:
        s = win.get((section, term))
        if s is None or not (
            s["window_start"] <= rank_run_mod.grid_start(period) <= s["window_end"]
        ):
            continue
        a = acc.setdefault(term, {"c": 0, "n": 0, "sic": [], "periods": []})
        a["c"] += int(c)
        a["n"] += int(n)
        a["sic"].append(payload)
        a["periods"].append(period)
    terms = {
        t: TermCounts(
            a["c"],
            a["n"],
            naics_mod.parse_sic_top(a["sic"]),
            f"{min(a['periods'])}..{max(a['periods'])}",
        )
        for t, a in acc.items()
    }
    cache: dict[str, tuple[dict[str, int], dict[str, int]]] = {}

    def counts(window: str) -> tuple[dict[str, int], dict[str, int]]:
        if window not in cache:
            p0, p1 = window.split("..")
            got = con.execute(
                "SELECT sic, count(*), count(DISTINCT entity) FROM text_structure "
                "WHERE source = 'edgar' AND sic IS NOT NULL AND period BETWEEN ? AND ? "
                "GROUP BY sic",
                [p0, p1],
            ).fetchall()
            cache[window] = ({r[0]: int(r[1]) for r in got}, {r[0]: int(r[2]) for r in got})
        return cache[window]

    return TextCtx(dict(tables.concordance), counts, terms)


def run_consequences(
    cfg: Mapping[str, Any], run_id: str | None = None, *, con: Any = None
) -> dict[str, Any]:
    """Calcula y escribe `rank/phenomenon_consequences` de una corrida (por defecto, la última)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from src.catalog import connect
    from src.config import REPO_ROOT
    from src.detect.run import _resolve
    from src.ingest.store import read_table, upsert
    from src.rank import naics as naics_mod
    from src.rank import run as rank_run_mod
    from src.rank.run import rank_root
    from src.schemas import (
        NAICS_LINKS,
        PHENOMENON_CHANGE,
        PHENOMENON_CONSEQUENCES,
        RANK_PHENOMENA,
        RANK_RUNS,
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
    links = read_table(
        NAICS_LINKS, rank_root(dict(cfg), "naics_links"), source=part, **where
    ).to_pylist()
    run_row = next(
        iter(
            read_table(
                RANK_RUNS, rank_root(dict(cfg), "rank_runs"), source=part, **where
            ).to_pylist()
        ),
        None,
    )
    changes: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in read_table(
        PHENOMENON_CHANGE, rank_root(dict(cfg), "phenomenon_change"), source=part, **where
    ).to_pylist():
        changes[r["phenomenon_id"]][r["change_type"]] = r
    own = con is None
    root = _resolve(cfg["paths"]["processed_dir"])
    con = con or connect(root)
    try:
        tables = naics_mod.load_tables(cfg, REPO_ROOT)
        mfile = root / "market" / "naics_metrics" / "part-0.parquet"
        market = dm.Market(pq.read_table(mfile).to_pylist() if mfile.exists() else [])
        ifile = root / "market" / "io_flows" / "part-0.parquet"
        io = IoFlows(pq.read_table(ifile).to_pylist()) if ifile.exists() else None
        p, dp = params_from(cfg), dm.params_from(cfg)
        # series ligadas a un NAICS por código (naics_links), con título y derechos
        keys = sorted({(r["source"], r["object_id"]) for r in links})
        con.register(
            "_clinks",
            pa.table({"source": [k[0] for k in keys], "series_id": [k[1] for k in keys]}),
        )
        meta = {
            (a, b): (t or "", bool(c))
            for a, b, t, c in con.execute(
                "SELECT m.source, m.series_id, m.title, m.copyrighted FROM series_meta m "
                "JOIN _clinks USING (source, series_id) WHERE NOT m.discontinued"
            ).fetchall()
        }
        keep = [re.compile(x) for x in (p.vacancies_regex, p.revenue_regex, p.expenses_regex)]
        by_naics: dict[str, list[SeriesRef]] = defaultdict(list)
        for r in links:
            k = (r["source"], r["object_id"])
            if k in meta and any(rx.search(meta[k][0]) for rx in keep) and r["peso"] >= 0.5:
                by_naics[r["naics"]].append(SeriesRef(k[0], k[1], meta[k][0], not meta[k][1]))
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

        text = _text_ctx(cfg, con, run_row, tables)
        out: list[dict[str, Any]] = []
        for ph in sorted(phens, key=lambda r: r["phenomenon_id"]):
            rows = build_consequences(
                ph,
                market,
                io=io,
                series=series,
                by_naics=by_naics,
                change_rows=changes.get(ph["phenomenon_id"]),
                text=text,
                dp=dp,
                p=p,
            )
            out += to_table_rows(run_id, ph["phenomenon_id"], rows)
    finally:
        if own:
            con.close()
    table = pa.Table.from_pylist(out, schema=PHENOMENON_CONSEQUENCES.schema)
    upsert(
        table,
        PHENOMENON_CONSEQUENCES,
        rank_root(dict(cfg), "phenomenon_consequences"),
        source=part,
        n_buckets=rank_run_mod.N_BUCKETS,
        bucket_col="run_id",
    )
    levels = {lv: sum(r["level"] == lv for r in out) for lv in (DIRECT, INDIRECT, NONE)}
    return {
        "run_id": run_id,
        "phenomena": len(phens),
        "rows": len(out),
        "io_loaded": io is not None,
        "text_loaded": text is not None,
        "levels": levels,
    }
