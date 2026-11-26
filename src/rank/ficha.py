"""Ficha del fenómeno (ADR 0036): contexto de industria aparte y 9 preguntas literales.

Pura composición de filas que ya existen (`phenomenon_dimensions`, `phenomenon_change`,
`phenomenon_consequences`): no calcula cifras nuevas, no usa IA ni redacta conclusiones.

- **Contexto de industria** (fuera de las 9): CR4/CR8, empresas, ingresos, entrada y salida, nivel
  frente al pico y precio frente al deflactor de la industria.
- **Cada pregunta** se responde solo con la evidencia que la contesta, con su nivel:
  `demostrado` ✅, `indicio` 🟡, `hipotesis` ⚪ o `sin_evidencia` (ninguna cifra la contesta).
  La Demanda sale de los compradores (insumo-producto, ADR 0032), no de las cifras de Evidencia;
  el Dinero es el gasto de esos compradores, no los ingresos de la industria (Contexto).
- **Confianza** cuenta los niveles de las otras ocho preguntas; no cuenta cifras.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from src.rank.change import SCARCITY
from src.schemas import Question

QUESTIONS = {d.name: d.value for d in Question}
LEVELS = ("demostrado", "indicio", "hipotesis", "sin_evidencia")
LEVEL_MARK = {"demostrado": "✅", "indicio": "🟡", "hipotesis": "⚪", "sin_evidencia": "—"}
LEVEL_NAME = {
    "demostrado": "demostrado",
    "indicio": "indicio",
    "hipotesis": "hipótesis",
    "sin_evidencia": "sin evidencia",
}
CONTEXT_METRICS = (
    ("competencia", "cr4"),
    ("competencia", "cr8"),
    ("competencia", "empresas"),
    ("demanda", "ingresos_industria"),
    ("entrada", "entry_rate"),
    ("entrada", "exit_rate"),
    ("entrada", "entry_rate_economia_total"),
    ("entrada", "exit_rate_economia_total"),
)
DIRECT, INDIRECT = "directo", "indirecto"


def _has(r: Mapping[str, Any]) -> bool:
    return r.get("level") in (DIRECT, INDIRECT) and (
        r.get("value") is not None or bool(r.get("value_text"))
    )


def _grade(figures: Sequence[Mapping[str, Any]]) -> str:
    """demostrado: alguna cifra directa; indicio: solo indirectas; si no, sin evidencia."""
    ok = [r for r in figures if _has(r)]
    if not ok:
        return "sin_evidencia"
    return "demostrado" if any(r["level"] == DIRECT for r in ok) else "indicio"


def _fig(r: Mapping[str, Any], label: str | None = None) -> dict[str, Any]:
    return {
        "label": label or r["metric"],
        "value": r.get("value"),
        "text": r.get("value_text"),
        "unit": r.get("unit"),
        "period": r.get("period"),
        "source": r.get("source_ref"),
        "level": r["level"],
        "note": r.get("note"),
    }


def build(
    phen: Mapping[str, Any],
    dims: Sequence[Mapping[str, Any]],
    change: Sequence[Mapping[str, Any]],
    cons: Sequence[Mapping[str, Any]],
    *,
    q: float = 0.05,
    min_sources: int = 2,
) -> dict[str, Any]:
    """`dims`/`change`/`cons`: filas públicas de las tres tablas del fenómeno."""
    d = {(r["dimension"], r["metric"]): r for r in dims}
    ch = {r["change_type"]: r for r in change}
    cons_by = lambda kind: [r for r in cons if r["consequence"] == kind]  # noqa: E731

    # --- Contexto de industria (fuera de las 9 preguntas)
    context = [_fig(d[k], f"{k[1]}") for k in CONTEXT_METRICS if k in d and _has(d[k])]
    context += [
        _fig(r)
        for r in dims
        if r["dimension"] == "brecha"
        and r["metric"].startswith(("nivel_vs_pico_previo", "precio_industria_vs_deflactor"))
        and _has(r)
    ]

    answers: dict[str, dict[str, Any]] = {}

    def put(dim: str, level: str, figures: list[dict[str, Any]], note: str | None = None) -> None:
        answers[dim] = {
            "dimension": dim,
            "question": QUESTIONS[dim],
            "level": level,
            "figures": figures,
            "note": note,
        }

    # 1. ¿Qué cambió?
    rows = [ch[t] for t in ("cantidad", "precio", "productividad", "composicion") if t in ch]
    known = [r for r in rows if r["status"] in ("si", "no")]
    figs = [
        _fig(
            {
                **r,
                "metric": r["change_type"],
                "value_text": f"{r['status']} · {r.get('note') or ''}",
            }
        )
        for r in known
    ]
    if not known:
        lvl = "sin_evidencia"
    elif any(r["status"] == "si" and r["level"] == DIRECT for r in known):
        lvl = "demostrado"
    else:
        lvl = "indicio"
    prod = ch.get("productividad")
    scarce = bool(prod and SCARCITY in (prod.get("note") or ""))
    put("fenomeno", lvl, figs, SCARCITY.capitalize() if scarce else None)

    # 2. ¿Qué lo prueba?  (series con señal tras FDR y fuentes independientes)
    sig = [
        r
        for r in dims
        if r["dimension"] == "evidencia"
        and r["metric"].startswith("q:")
        and r.get("value") is not None
        and r["value"] <= q
    ]
    n_src = len(phen.get("sources") or [])
    if not sig:
        lvl = "sin_evidencia"
    elif n_src >= min_sources:
        lvl = "demostrado"
    else:
        lvl = "indicio"
    put(
        "evidencia",
        lvl,
        [_fig(r, r["metric"].removeprefix("q:")) for r in sig],
        f"{n_src} fuente(s) independiente(s)",
    )

    # 3. ¿Quién necesita algo como consecuencia?  (solo compradores)
    buyers = [
        r
        for r in cons_by("compradores")
        if r["metric"].startswith(("demanda_", "comprador_")) and _has(r)
    ]
    put("demanda", _grade(buyers), [_fig(r) for r in buyers])

    # 4. ¿Qué no resuelve la oferta?  (escasez; vacantes demuestran, el resto es indicio)
    scar = [r for r in cons_by("escasez") if _has(r)]
    vac = [r for r in scar if r["metric"].startswith("vacantes") and r["level"] == DIRECT]
    if vac:
        lvl = "demostrado"
    elif scar:
        lvl = "indicio"
    elif scarce:
        lvl = "hipotesis"
    else:
        lvl = "sin_evidencia"
    put(
        "brecha",
        lvl,
        [_fig(r) for r in scar],
        f"{SCARCITY.capitalize()}: el empleo cae y la productividad no es una mejora"
        if scarce
        else None,
    )

    # 5. ¿Quién la atiende hoy?  (composición del mercado y pérdida de participación)
    comp = (
        [ch["composicion"]]
        if "composicion" in ch and ch["composicion"]["status"] != "sin_dato"
        else []
    )
    share = [r for r in cons_by("perdida_participacion") if _has(r)]
    figs = [_fig({**r, "metric": "composicion", "value_text": r.get("note")}) for r in comp] + [
        _fig(r) for r in share
    ]
    lvl = (
        "demostrado"
        if any(r["level"] == DIRECT for r in comp + share)
        else "indicio"
        if figs
        else "sin_evidencia"
    )
    put("competencia", lvl, figs)

    # 6. ¿Cuánto gastan hoy los compradores?  (gasto en dólares, insumo-producto)
    spend = [r for r in cons_by("compradores") if r["metric"].startswith("gasto_") and _has(r)]
    put("dinero", _grade(spend), [_fig(r) for r in spend])

    # 7. ¿Qué cuesta entrar?  (tamaño típico de un establecimiento y márgenes)
    size = [
        d[("dinero", m)]
        for m in ("ingreso_por_establecimiento", "ingreso_por_empresa")
        if ("dinero", m) in d and _has(d[("dinero", m)])
    ]
    marg = [r for r in cons_by("margenes") if _has(r)]
    figs = [_fig(r) for r in size + marg]
    lvl = (
        "demostrado"
        if size and any(r["level"] == DIRECT for r in marg)
        else "indicio"
        if figs
        else "sin_evidencia"
    )
    put("entrada", lvl, figs)

    # 8. ¿Qué puede salir mal?  (problemas declarados en reportes de la industria)
    risk = [r for r in cons_by("problemas") if _has(r)]
    put("riesgo", _grade(risk), [_fig(r) for r in risk])

    # 9. ¿Cuánto de esto está demostrado?  (conteo de niveles de las otras ocho preguntas)
    counts = Counter(a["level"] for a in answers.values())
    put(
        "confianza",
        "sin_evidencia" if not any(counts[k] for k in LEVELS[:3]) else "indicio",
        [],
        None,
    )
    answers["confianza"]["counts"] = {lv: counts[lv] for lv in LEVELS}
    answers["confianza"]["level"] = None  # no es una pregunta con cifras propias
    return {
        "context": context,
        "answers": [answers[k] for k in QUESTIONS],
        "scarcity": scarce,
    }


def to_markdown(f: Mapping[str, Any]) -> str:
    """La ficha tal como sale en el reporte, en texto."""

    def fmt(x: Mapping[str, Any]) -> str:
        v = "" if x["value"] is None else f"{x['value']:,.2f}"
        t = f" {x['text']}" if x.get("text") else ""
        u = f" {x['unit']}" if x.get("unit") else ""
        per = f" · {x['period']}" if x.get("period") else ""
        src = f" · {x['source']}" if x.get("source") else ""
        return f"{x['label']}: {v}{u}{t}{per}{src} ({x['level']})".replace("  ", " ")

    out = ["### Contexto de industria", ""]
    out += [f"- {fmt(x)}" for x in f["context"]] or ["- sin cifras de contexto"]
    out += ["", "### Las 9 preguntas", ""]
    for a in f["answers"]:
        if a["dimension"] == "confianza":
            c = a["counts"]
            line = " · ".join(f"{LEVEL_MARK[k]} {LEVEL_NAME[k]}: {c[k]}" for k in LEVELS)
            out += [f"**{QUESTIONS['confianza']}** — {line}", ""]
            continue
        head = f"**{a['question']}** — {LEVEL_MARK[a['level']]} {LEVEL_NAME[a['level']]}"
        out.append(head + (f" · {a['note']}" if a.get("note") else ""))
        out += [f"  - {fmt(x)}" for x in a["figures"]]
        if not a["figures"]:
            out.append("  - sin evidencia: ninguna cifra responde la pregunta")
        out.append("")
    return "\n".join(out)
