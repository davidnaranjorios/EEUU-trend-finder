"""Páginas HTML del sitio: ranking semanal, dashboard y archivo de reportes."""

from __future__ import annotations

import html
import json
import math
from collections import defaultdict
from typing import Any

from src.report.recent import INSUFFICIENT, STATES

DETECTOR_NAMES = {
    "slope": "cambio de pendiente",
    "regime": "quiebre de régimen",
    "text_emerging": "término emergente",
}


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def fq(q: float | None) -> str:
    if q is None:
        return "—"
    return f"{q:.2e}" if q < 1e-3 else f"{q:.4f}"


def fnum(v: float | int | None, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    if isinstance(v, int):
        return f"{v:,}".replace(",", ".")
    return f"{v:.{digits}f}"


def src_name(s: str) -> str:
    """Nombre visible de una fuente a partir de su código (fred -> FRED, federal_register ->
    Federal Register)."""
    return s.upper() if "_" not in s and len(s) <= 6 else s.replace("_", " ").title()


def concat(*parts: str) -> str:
    return "\n".join(parts)


CURRENT = ' aria-current="page"'


def page(title: str, current: str, body: str, generated: str) -> str:
    nav = "".join(
        f'<a href="{href}"{CURRENT if href == current else ""}>{label}</a>'
        for href, label in (
            ("index.html", "Ranking"),
            ("dashboard.html", "Dashboard"),
            ("archivo.html", "Archivo"),
        )
    )
    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="assets/site.css">
</head>
<body>
<header class="top"><div class="wrap"><h1>Tendencias EE. UU.</h1><nav>{nav}</nav></div></header>
<main class="wrap">
{body}
<footer>Generado {esc(generated)}. El sistema propone candidatos; la interpretación es humana.
Las series con copyright de terceros no se publican.</footer>
</main>
<script src="assets/site.js"></script>
</body>
</html>
"""


def _table(rows: list[list[Any]], num: set[int], *head: str) -> str:
    th = "".join(f"<th{' class=num' if i in num else ''}>{esc(h)}</th>" for i, h in enumerate(head))
    trs = "".join(
        "<tr>"
        + "".join(f"<td{' class=num' if i in num else ''}>{esc(c)}</td>" for i, c in enumerate(r))
        + "</tr>"
        for r in rows
    )
    return (
        f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead>'
        f"<tbody>{trs}</tbody></table></div>"
    )


def _sources_chips(groups: list[list[str]]) -> str:
    return "".join(
        f'<span class="chip">{esc(" + ".join(agency_label(a) for a in g))}</span>' for g in groups
    )


def _why_sources(groups: list[list[str]]) -> str:
    names = ["; ".join(agency_label(a) for a in g) for g in groups]
    return (
        f"Cuenta como {len(groups)} fuentes independientes: la evidencia viene de "
        + " y de ".join(names)
        + ", y ninguna serie ni término une a esos grupos (agencias que publican el mismo dato "
        "se cuentan como una sola)."
    )


def guide(
    weights: dict[str, float], q: float, min_sources: int, recent: dict[str, Any] | None = None
) -> str:
    """Cómo leer la página y glosario (pesos y umbrales tomados de config.yaml)."""
    total = sum(weights.values()) or 1.0
    rc = recent or {
        "months": 12,
        "baseline_years": 5,
        "accel_ratio": 1.5,
        "slow_ratio": 0.5,
        "reverse_frac": 0.25,
    }

    def item(k: str, text: str) -> str:
        w = weights.get(k, 0.0) / total
        return f"<li><strong>{esc(k)}</strong> ({w:.0%}): {esc(text)}</li>"

    rows = concat(
        item("strength", "fuerza estadística: −log10 q, mediana por fuente independiente"),
        item("n_sources", "número de fuentes independientes"),
        item("effect", "tamaño del efecto de las series"),
        item("agreement", "si los dos detectores numéricos coinciden"),
        item("breadth", "entidades distintas (empresas o agencias) que usan el término"),
        item("size", "miembros del cluster (con rendimiento decreciente)"),
    )
    return concat(
        '<section class="card guide" id="como-leer"><h2>Cómo leer esta página</h2>',
        "<p>Cada tarjeta es un <strong>candidato</strong>: un grupo de series (y términos de "
        "texto) que cambiaron de forma persistente. La frase en lenguaje simple resume el cambio "
        "con los datos de la serie; el gráfico muestra los valores en sus unidades originales, "
        "con la ventana de la señal sombreada. Un candidato solo aparece si lo respaldan al menos "
        f"{min_sources} fuentes independientes. El sistema propone; la interpretación es "
        "humana.</p>",
        '<details open id="glosario"><summary><strong>Glosario</strong></summary>'
        '<dl class="gloss">',
        "<dt>Efecto</dt><dd>Tamaño del cambio. En series, en desviaciones estándar robustas del "
        "crecimiento; en términos, en logit de la proporción de documentos.</dd>",
        "<dt>p</dt><dd>Probabilidad de ver un cambio así o mayor si no hubiera cambio real, "
        "calibrada con bootstrap por bloques (respeta la autocorrelación).</dd>",
        f"<dt>q</dt><dd>p ajustado por comparaciones múltiples (BH). Se aceptan señales con "
        f"q ≤ {q}: en promedio, a lo sumo {q:.0%} de ellas serían falsas.</dd>",
        "<dt>BH</dt><dd>Benjamini-Hochberg: el método que decide qué señales pasan.</dd>",
        "<dt>BY</dt><dd>Benjamini-Yekutieli: versión más estricta, válida con cualquier "
        "dependencia. Se reporta solo como comparación.</dd>",
        "<dt>Últimos 12 meses</dt><dd>Contexto descriptivo, no una señal: sin p ni q, no cambia "
        f"el ranking. Compara el ritmo anualizado de los últimos {rc['months']} meses con la "
        f"pendiente de {rc['baseline_years']} años: <em>acelera</em> (≥ {rc['accel_ratio']} "
        f"veces la tendencia), <em>se frena</em> (≤ {rc['slow_ratio']} veces), "
        f"<em>revierte</em> (signo contrario y al menos {rc['reverse_frac']} veces), "
        "<em>continúa</em> (el resto). "
        "Solo para series mensuales o más frecuentes; los datos recientes son preliminares.</dd>",
        "<dt>Efecto de tasas</dt><dd>Etiqueta descriptiva, no una señal ni un filtro: el "
        "crecimiento del cluster se relaciona de forma significativa con el nivel y los cambios "
        "de la tasa de corto plazo (T-bill a 3 meses) y, al quitar esa parte, su cambio de "
        "pendiente cae o deja de ser significativo. El candidato sigue en el ranking con el "
        "mismo score.</dd>",
        "<dt>Clase de medida</dt><dd>Qué mide cada serie: precio (incluido el costo laboral), "
        "cantidad, valor nominal (precio × cantidad) o tasa. Sale de las unidades y de los "
        "códigos oficiales de la agencia, nunca del tema.</dd>",
        "<dt>Sin evidencia de demanda</dt><dd>Etiqueta de un fenómeno cuyo respaldo numérico son "
        "solo precios: puede reflejar costos u oferta. No es un filtro ni cambia el ranking.</dd>",
        "<dt>Efecto inflación</dt><dd>Etiqueta descriptiva, no una señal ni un filtro: el "
        "crecimiento del cluster se relaciona de forma significativa con la inflación (IPC-U) y, "
        "al quitar esa parte, su cambio de pendiente cae o deja de ser significativo. El "
        "candidato sigue en el ranking con el mismo score.</dd>",
        "<dt>En observación</dt><dd>Lista separada del ranking (🟡): fenómenos de un solo origen "
        "que pasan el control de falsos descubrimientos y tienen evidencia de demanda (series de "
        "cantidad o valor sin efecto de tasas ni de inflación). No están triangulados, no entran "
        "al score y no se presentan como candidatos del ranking.</dd>",
        "<dt>Ficha del fenómeno</dt><dd>Contexto descriptivo, no una señal: las series del "
        "cluster en base 100, transformaciones (interanual, anualizada, real y per cápita solo "
        "con unidades nominales), relacionadas elegidas por los datos (misma rama de categoría, "
        "título parecido y crecimientos correlacionados; el rol sale de las unidades), rezagos y "
        "sensibilidad a la ventana y a las revisiones.</dd>",
        "<dt>Fuente independiente</dt><dd>Grupo de agencias que no comparten datos: el mismo dato "
        "publicado por FRED y por la agencia original cuenta una vez.</dd>",
        f"<dt>Score</dt><dd>Promedio ponderado de percentiles dentro de la corrida:<ul>{rows}"
        "</ul></dd>",
        "</dl></details></section>",
    )


TIPS = {
    "q": "q de Benjamini-Hochberg: proporción esperada de falsos descubrimientos si se aceptan "
    "todas las señales con un q igual o menor. Más chico = más confiable.",
    "efecto_sd": "Tamaño del cambio en desviaciones estándar robustas del crecimiento de la serie "
    "(comparable entre series con unidades distintas).",
    "efecto_logit": "Aumento del logit de la proporción de documentos que mencionan el término "
    "(ventana reciente frente a la línea base).",
    "reciente": "Lectura de los datos más recientes: no es una señal estadística (sin p ni q) "
    "y no cambia el ranking ni el score.",
    "similitud": "Coseno entre los embeddings del término y del título de la serie; solo se "
    "enlaza si supera lo que alcanza por azar la mejor coincidencia de un término común.",
}


def tip(label: str, key: str) -> str:
    return f'<abbr title="{esc(TIPS[key])}">{esc(label)}</abbr>'


def agency_label(key: str) -> str:
    if key.startswith("texto:"):
        return "textos de " + src_name(key.removeprefix("texto:"))
    return key[:1].upper() + key[1:]


def _legend(has_window: bool, pelt: Any) -> str:
    items = []
    if has_window:
        items.append('<span class="key win"></span> ventana de la señal')
    if pelt:
        items.append(f'<span class="key mark"></span> inicio del régimen (PELT): {esc(pelt)}')
    return f'<p class="legend">{" · ".join(items)}</p>' if items else ""


def _origin_rows(s: dict[str, Any]) -> str:
    out = ""
    if s.get("origin_chain"):
        out += f"<dt>Origen del dato</dt><dd>{esc(s['origin_chain'])}</dd>\n"
    if s.get("possible_same_origin"):
        names = ", ".join(s["possible_same_origin"])
        out += (
            '<dt>Posible mismo origen</dt><dd class="muted">Serie de otra agencia con crecimiento '
            f"y nivel casi idénticos ({esc(names)}): cuenta como una sola fuente; pendiente de "
            "revisión humana.</dd>\n"
        )
    return out


# Tooltip de la clase de medida (fuera de TIPS: ese diccionario es de estadística, no de medida).
CLASS_TIP = (
    "Qué mide la serie: precio, cantidad, valor nominal (precio × cantidad), tasa o razón. Sale "
    "de las unidades y los códigos oficiales de la agencia, no del tema. Es descriptiva: no "
    "cambia el ranking ni el score."
)
MEASURE_LABELS = {
    "precio": "precio",
    "cantidad": "cantidad",
    "valor": "valor (nominal)",
    "tasa": "tasa o razón",
    "otro": "sin clasificar",
}


def measure_label(kind: str | None, subtype: str | None = None) -> str:
    """ "precio (costo laboral)", "cantidad"...: la clase de medida de una serie (ADR 0014)."""
    base = MEASURE_LABELS.get(kind or "otro", "sin clasificar")
    return f"{base} (costo laboral)" if subtype == "costo_laboral" else base


def _measure_mix(mix: dict[str, int]) -> str:
    return ", ".join(f"{n} {MEASURE_LABELS.get(k, k)}" for k, n in mix.items()) or "—"


def _series_block(s: dict[str, Any]) -> str:
    dets = ", ".join(DETECTOR_NAMES.get(d, d) for d in s["detectors"])
    w = s["window"]
    agencies = ", ".join(agency_label(a) for a in s["agencies"]) or "—"
    sentences = "".join(f'<p class="plain">{esc(x)}</p>' for x in s.get("sentences", []))
    caption = (
        f'<p class="cap"><strong>{esc(s["title"])}</strong><br>'
        f"{esc(s['units']) or 'sin unidades'} · {esc(s['seasonal_adjustment']) or '—'} · "
        f"{esc(src_name(s['source']))} ({esc(agencies)})</p>"
    )
    return f"""<div class="ev">
<div><h3>{esc(s["title"])}</h3>{sentences}
<dl><dt>Fuente</dt><dd>{esc(src_name(s["source"]))} · {esc(s["id"])}</dd>
<dt>Agencia</dt><dd>{esc(agencies)}</dd>
{_origin_rows(s)}<dt><abbr title="{esc(CLASS_TIP)}">Clase</abbr></dt>
<dd>{esc(measure_label(s.get("measure_kind"), s.get("measure_subtype")))}</dd><dt>Unidades</dt>
<dd>{esc(s["units"])} ({esc(s["frequency"])}, {esc(s["seasonal_adjustment"])})</dd>
<dt>Detector</dt><dd>{esc(dets)}</dd>
<dt>{tip("q (BH)", "q")}</dt><dd>{fq(s["q_value"])}</dd>
<dt>{tip("Efecto", "efecto_sd")}</dt><dd>{fnum(s["effect_size"])} sd</dd>
<dt>Ventana</dt><dd>{esc(w[0])} → {esc(w[1])}</dd></dl></div>
<figure>{caption}{s["chart"]}{_legend(bool(w[0]), s.get("pelt"))}</figure>
</div>{_recent_block(s)}"""


def _linked(key: str) -> tuple[str, str]:
    src, sid = key.split("|", 1)
    return src_name(src), sid


def ts(v: Any) -> str:
    """Fecha y hora hasta el minuto."""
    return str(v or "")[:16].replace("T", " ")


def _term_block(t: dict[str, Any]) -> str:
    w = t["window"]
    link = ""
    if t["link_score"] is not None:
        target = esc(" · ".join(_linked(t["linked_series"]))) if t["linked_series"] else "una serie"
        link = (
            f"<dt>{tip('Similitud', 'similitud')}</dt>"
            f"<dd>{fnum(t['link_score'], 3)} con {target}</dd>"
        )
    ent = f"<dt>Entidades</dt><dd>{fnum(t['n_entities'])}</dd>" if t["n_entities"] else ""
    plain = f'<p class="plain">{esc(t["sentence"])}</p>' if t.get("sentence") else ""
    return f"""<div class="ev">
<div><h3>“{esc(t["term"])}”</h3>{plain}
<dl><dt>Fuente</dt><dd>{esc(src_name(t["source"]))} · sección {esc(t["section"])}</dd>
<dt>{tip("q (BH)", "q")}</dt><dd>{fq(t["q_value"])}</dd>
<dt>{tip("Efecto", "efecto_logit")}</dt><dd>{fnum(t["effect_size"])} logit</dd>{ent}{link}
<dt>Ventana</dt><dd>{esc(w[0])} → {esc(w[1])}</dd></dl></div>
<figure><p class="cap"><strong>Proporción de documentos con “{esc(t["term"])}”</strong><br>
% de documentos por periodo · {esc(src_name(t["source"]))}, sección {esc(t["section"])}</p>
{t["chart"]}{_legend(bool(w[0]), None)}</figure>{_term_recent(t)}
</div>"""


STATE_OPTIONS = (*STATES, "mixto", INSUFFICIENT)
PRELIM = "Preliminar: los meses recientes se revisan."


def _rate(r: Any) -> str:
    if not r:
        return "—"
    value, pct = r
    return f"{value:+.1f} %" if pct else f"{value:+.3g}"


def _recent_block(s: dict[str, Any]) -> str:
    """Zoom de 12 meses (descriptivo: sin p ni q, no entra al ranking)."""
    r = s.get("recent") or {}
    if not r:
        return ""
    note = f'<p class="muted">{esc(r["note"])}</p>' if r.get("note") else ""
    chart = (
        f'{r["chart"]}<p class="legend"><span class="key win"></span> últimos 12 meses</p>'
        if r.get("chart")
        else ""
    )
    return f"""<div class="recent"><h4>Últimos 12 meses ·
<span class="state" data-state="{esc(r["state"])}">{esc(r["state"])}</span></h4>
<p class="muted">{tip("Contexto descriptivo", "reciente")}. {PRELIM}</p>{note}
<dl><dt>Interanual</dt><dd>{_rate(r.get("yoy"))} (último dato vs 12 meses antes)</dd>
<dt>Ritmo 12 meses</dt><dd>{_rate(r.get("recent_rate"))} anualizado</dd>
<dt>Tendencia 5 años</dt><dd>{_rate(r.get("trend_rate"))} anualizado</dd></dl>{chart}</div>"""


def _term_recent(t: dict[str, Any]) -> str:
    r = t.get("recent")
    if not r:
        return ""
    last, prev = r
    return (
        f'<p class="plain recent">Último año: {last * 100:.2f} % de los documentos, frente a '
        f"{prev * 100:.2f} % el año anterior ({tip('contexto descriptivo', 'reciente')}).</p>"
    )


SA_NOTE = " · desestacionalizada en el gráfico"


STACK_DEFAULT = 3  # relacionadas visibles al abrir el panel


def _stack(entries: list[dict[str, Any]]) -> str:
    """Panel de gráficos apilados (mismo eje de fechas) con selector de relacionadas."""
    if len(entries) < 2:
        return ""
    boxes, charts = [], []
    for i, e in enumerate(entries):
        on = e["core"] or i <= STACK_DEFAULT
        if not e["core"]:
            boxes.append(
                f'<label><input type="checkbox" data-key="{esc(e["key"])}"'
                f"{' checked' if on else ''}> {esc(e['role'])}: {esc(e['title'])}</label>"
            )
        charts.append(
            f'<figure class="stack-item" data-key="{esc(e["key"])}"{"" if on else " hidden"}>'
            f'<p class="cap"><strong>{esc(e["title"])}</strong><br>{esc(e["units"]) or "—"} · '
            f"{esc(src_name(e['source']))} · {esc(e['id'])} · {esc(e['role'])}</p>{e['chart']}"
            "</figure>"
        )
    return concat(
        "<h4>Comparar con relacionadas (mismo eje de fechas)</h4>",
        '<p class="muted">El núcleo va siempre arriba; cada gráfico usa sus propias unidades. '
        "Marca las relacionadas que quieras ver.</p>",
        '<div class="stack"><div class="stack-pick">',
        *boxes,
        "</div>",
        *charts,
        "</div>",
    )


def _phenomenon(p: dict[str, Any] | None) -> str:
    """Ficha del fenómeno (descriptiva: sin p ni q; no cambia el ranking)."""
    if not p:
        return ""
    head = (
        f'<p class="muted">{tip("Contexto descriptivo", "reciente")}: no es una señal ni cambia el '
        "ranking.</p>"
    )
    core = ""
    if p["chart"]:
        rec = " Zonas grises: recesiones." if p["has_recessions"] else ""
        core = (
            f"<h4>Núcleo: series del cluster en base 100 (= {esc(p['base'])})</h4>"
            f'<p class="muted">Cambio fechado en {esc(p["change_date"])}.{rec}</p>{p["chart"]}'
        )
    trows = [
        [
            t["title"]
            + (" · desestacionalizada en el gráfico" if t["seasonally_adjusted_here"] else ""),
            f"{fnum(t['last'], 2)} {t['units']} ({t['last_date']})",
            t["yoy"] or "—",
            t["annual"] or "—",
            t["real_annual"] or ("no aplica" if not t["nominal"] else "—"),
            fnum(t["per_capita"], 2)
            if t["per_capita"] is not None
            else ("no aplica" if not t["nominal"] else "—"),
        ]
        for t in p["transforms"]
    ]
    rrows = [
        [
            r["role"],
            r["title"],
            f"{src_name(r['source'])} · {r['series_id']}",
            fnum(r["score"], 3),
            "—" if r["rho"] is None else fnum(r["rho"], 2),
            "—" if r["lag"] is None else f"{r['lag']} ({r['grid']}), ρ {fnum(r['lag_corr'], 2)}",
            "descargada" if r["downloaded"] else "no descargada",
        ]
        for r in p["related"]
    ]
    wrows = [
        [
            w["title"],
            *[w["windows"].get(k) or "—" for k in sorted(w["windows"])],
            "—"
            if not w["revision"]
            else f"{w['revision']['then']} (vintage {w['revision']['vintage']}) → "
            f"{w['revision']['now']}",
        ]
        for w in p["robust"]
    ]
    years = sorted(p["robust"][0]["windows"]) if p["robust"] else []
    missing = (
        '<p class="muted">Series de referencia no descargadas: '
        f"{esc(', '.join(p['missing_refs']))}.</p>"
        if p["missing_refs"]
        else ""
    )
    return concat(
        '<details class="pheno"><summary><strong>Ficha del fenómeno</strong></summary>',
        head,
        core,
        "<h4>Transformaciones</h4>",
        _table(
            trows,
            {2, 3, 4, 5},
            "Serie",
            "Último dato",
            "Interanual",
            "Anualizada 5 años",
            "Real anualizada",
            "Per cápita (último)",
        ),
        '<p class="muted">Real y per cápita solo con unidades nominales (dólares corrientes).</p>',
        missing,
        "<h4>Relacionadas (elegidas por los datos)</h4>",
        _table(
            rrows,
            {3, 4},
            "Rol",
            "Serie",
            "Fuente",
            "Puntaje",
            "ρ crecim.",
            "Rezago (periodos)",
            "Estado",
        )
        if rrows
        else '<p class="muted">Sin series de la misma rama.</p>',
        '<p class="muted">Rezago descriptivo: > 0 significa que la relacionada va detrás del '
        "núcleo.</p>",
        _stack(p.get("stack") or []),
        "<h4>Robustez</h4>",
        _table(
            wrows,
            set(range(1, len(years) + 1)),
            "Serie",
            *[f"Pendiente {y} años" for y in years],
            "Revisiones (vintages)",
        )
        if wrows
        else "",
        "</details>",
    )


def _naics_line(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<p class="muted">Industria (NAICS): sin anclaje.</p>'
    txt = "; ".join(
        f"{i['naics']} {i['name'] or ''} ({i['weight']:.0%}, {'/'.join(i['methods'])}"
        f"{', estimado' if i.get('estimated') else ''})"
        for i in items
    )
    return f'<p class="plain naics">Industria (NAICS): {esc(txt)}</p>'


def _naics_coverage(rr: dict[str, Any]) -> str:
    try:
        cov = json.loads(rr.get("params_json") or "{}")["summary"]["naics_coverage"]
    except (KeyError, ValueError, TypeError):
        return ""
    return (
        "<details><summary>Cobertura NAICS (señales con industria)</summary>"
        + _table(
            [[s, v["n"], v["with_naics"], f"{v['pct']:.1f}%"] for s, v in cov.items()],
            {1, 2, 3},
            "Fuente",
            "Señales",
            "Con NAICS",
            "%",
        )
        + "</details>"
    )


def _rate_badge(c: dict[str, Any]) -> str:
    """Etiqueta "efecto de tasas": solo avisa, nunca filtra ni cambia el score."""
    if not c.get("rate_effect"):
        return ""
    share = c.get("rate_share")
    pct = f" ({share:.0%} del cambio de pendiente)" if share is not None else ""
    title = (
        "El crecimiento de este cluster se explica en buena parte por los cambios de la tasa "
        "de corto plazo (T-bill a 3 meses): al quitarlos, el cambio de pendiente cae o deja de "
        "ser significativo. Etiqueta descriptiva: no cambia el ranking."
    )
    return f' <span class="state rate-effect" title="{esc(title)}">Efecto de tasas{esc(pct)}</span>'


def _inflation_badge(c: dict[str, Any]) -> str:
    """Etiqueta "efecto inflación": solo avisa, nunca filtra ni cambia el score."""
    if not c.get("inflation_effect"):
        return ""
    share = c.get("inflation_share")
    pct = f" ({share:.0%} del cambio de pendiente)" if share is not None else ""
    title = (
        "El crecimiento de este cluster se explica en buena parte por la inflación (IPC-U): al "
        "quitarla, el cambio de pendiente cae o deja de ser significativo. Etiqueta "
        "descriptiva: no cambia el ranking."
    )
    return (
        f' <span class="state inflation-effect" title="{esc(title)}">'
        f"Efecto inflación{esc(pct)}</span>"
    )


def _candidate(c: dict[str, Any]) -> str:
    hidden = (
        f'<p class="muted">{c["hidden_series"]} serie(s) del cluster con copyright de terceros '
        "no se muestran.</p>"
        if c["hidden_series"]
        else ""
    )
    comp = ", ".join(f"{k} {v:.2f}" for k, v in c["components"].items())
    ev = "".join(_series_block(s) for s in c["series"]) + "".join(
        _term_block(t) for t in c["terms"]
    )
    state = c.get("state") or ""
    badge = (
        f'<span class="state" data-state="{esc(state)}">Últimos 12 meses: {esc(state)}</span>'
        if c["kind"] == "series"
        else ""
    )
    naics = _naics_line(c.get("naics") or [])
    rate = _rate_badge(c) + _inflation_badge(c)
    return f"""<section class="card cand" id="{esc(c["candidate_id"])}" data-rank="{c["rank"]}"
 data-state="{esc(state)}">
<header><span class="rank">#{c["rank"]}</span>{badge}
<strong>{c["n_sources"]} fuentes independientes</strong>
<span>score {fnum(c["score"], 3)}</span>
<span class="muted">{c["n_members"]} miembro(s) · {len(c["terms"])} término(s)</span>{rate}</header>
<div>{_sources_chips(c["sources"])}</div>
{naics}
<p class="plain">{esc(_why_sources(c["sources"]))}</p>
<details><summary>Componentes del score</summary><p class="muted">{esc(comp)}</p></details>
{hidden}{ev}{_phenomenon(c.get("phenomenon"))}
</section>"""


def _phenomenon_card(p: dict[str, Any]) -> str:
    naics = "; ".join(f"{n['naics']} {n['name'] or ''} ({n['weight']:.0%})" for n in p["naics"])
    clusters = " ".join(
        f'<a href="#{esc(c["id"])}"><code>{esc(c["id"])}</code></a>'
        if c["in_ranking"]
        else f'<code title="cluster de una sola fuente, sin ficha propia">{esc(c["id"])}</code>'
        for c in p["clusters"]
    )
    mix = esc(_measure_mix(p.get("measure_mix") or {}))
    demand = (
        '<p class="plain demand-warning"><strong>Sin evidencia de demanda:</strong> todo su '
        "respaldo numérico son precios (sin cantidad ni valor real). Un cambio de precios puede "
        "reflejar costos u oferta y no demanda. Etiqueta descriptiva: no cambia el ranking.</p>"
        if p.get("no_demand_evidence")
        else ""
    )
    grouping = (
        '<p class="plain">Ningún cluster llega solo a 2 fuentes independientes: la triangulación '
        "sale de juntarlos (misma industria, misma dirección, ventanas que se solapan).</p>"
        if p["by_grouping"]
        else ""
    )
    ws, we = p["window"]
    win = f" · ventana {esc(ws)} a {esc(we)}" if ws and we else ""
    return f"""<section class="card phen" id="{esc(p["phenomenon_id"])}">
<header><span class="rank">#{p["rank"]}</span>
<strong>{p["n_sources"]} fuentes independientes</strong> <span>score {fnum(p["score"], 3)}</span>
<span class="muted">{p["n_clusters"]} cluster(s) · {p["n_members"]} serie(s) · \
{esc(p["direction"])}{win}</span></header>
<div>{_sources_chips(p["sources"])}</div>
<p class="plain naics">Industria (NAICS): {esc(naics) or "sin anclaje"}</p>
{demand}{grouping}<p class="muted">Clases de medida: {mix}</p>
<p class="muted">Clusters: {clusters}</p>
</section>"""


def _watch_card(p: dict[str, Any]) -> str:
    naics = "; ".join(f"{n['naics']} {n['name'] or ''} ({n['weight']:.0%})" for n in p["naics"])
    ws, we = p["window"]
    win = f" · ventana {esc(ws)} a {esc(we)}" if ws and we else ""
    clusters = " ".join(f"<code>{esc(c)}</code>" for c in p["clusters"])
    re_ = p.get("real_effect")
    real = f" (real, deflactado: {fnum(abs(re_), 2)})" if re_ is not None else ""
    return f"""<section class="card watch" id="{esc(p["phenomenon_id"])}">
<header><span class="state watch-flag">🟡 En observación</span>
<span>efecto {fnum(p["effect"], 2)}{real} · q {fnum(p["q"], 4)}</span>
<span class="muted">{p["n_clusters"]} cluster(s) · {p["n_members"]} serie(s) · \
{esc(p["direction"])}{win}</span></header>
<p class="plain watch-warning"><strong>Un solo origen: no triangulado.</strong> No es un candidato \
del ranking ni entra al score; pasa FDR y tiene {p["n_demand_series"]} serie(s) de cantidad o \
valor real (el valor nominal solo cuenta si su crecimiento deflactado conserva el efecto), sin \
efecto de tasas ni de inflación.</p>
<div>{_sources_chips(p["sources"])}</div>
<p class="plain naics">Industria (NAICS): {esc(naics) or "sin anclaje"}</p>
<p class="muted">Clases de medida: {esc(_measure_mix(p.get("measure_mix") or {}))}</p>
<p class="muted">Clusters: {clusters}</p>
</section>"""


def _tile(k: str, v: Any) -> str:
    return f'<div class="tile"><div class="v">{esc(v)}</div><div class="k">{esc(k)}</div></div>'


def ranking_page(data: dict[str, Any]) -> str:
    rr = data["rank_run"]
    det = data["status"]["detectors"]
    tiles = concat(
        f'<p class="muted" id="rank-run">Corrida: <code>{esc(rr.get("run_id"))}</code></p>',
        '<div class="tiles">',
        _tile("Series con señal", fnum(rr.get("n_signal_series"))),
        _tile("Términos con señal", fnum(rr.get("n_signal_terms"))),
        _tile("Clusters", fnum(rr.get("n_clusters"))),
        _tile("Fenómenos", fnum(len(data.get("phenomena") or []))),
        _tile("Con ≥ 2 fuentes", fnum(rr.get("n_candidates"))),
        _tile("En observación (1 origen)", fnum(len(data.get("watchlist") or []))),
        _tile("Publicados", fnum(rr.get("n_published"))),
        _tile("Una sola fuente (descartados)", fnum(rr.get("n_single_source"))),
        "</div>",
        _naics_coverage(rr),
    )
    det_rows = [
        [
            d["family"],
            DETECTOR_NAMES.get(d["detector"], d["detector"]),
            d["n_tests"],
            d["n_bh"],
            f"{d['bh_rate']:.2%}" + (" ⚠ más de 3× la mediana de los demás" if d["alert"] else ""),
            d["n_by"],
            d["q"],
        ]
        for d in det
    ]
    series = data["candidates"]["series"]
    text = data["candidates"]["text"]
    none = '<p class="muted">Sin candidatos esta semana.</p>'
    body = concat(
        "<h2>Candidatos a tendencias estructurales</h2>",
        '<p class="lead">Clusters de series y términos con señal estadística (FDR controlada con '
        "Benjamini-Hochberg) respaldados por al menos dos fuentes independientes.</p>",
        tiles,
        guide(data["weights"], data["q"], data["min_sources"], data.get("recent")),
        "<h2>Señales por detector</h2>",
        _table(det_rows, {2, 3, 5, 6}, "Familia", "Detector", "Pruebas", "BH", "% BH", "BY", "q"),
        '<p class="muted">BH decide; BY se reporta solo como comparación.</p>',
        f"<h2>Fenómenos ({len(data.get('phenomena') or [])})</h2>",
        '<p class="lead">Un fenómeno agrupa clusters de la misma industria (NAICS), con el mismo '
        "sentido del cambio y ventanas que se solapan. Se triangula sobre la evidencia de todos "
        "sus clusters, contando fuentes por origen del dato.</p>",
        concat(*(_phenomenon_card(p) for p in data.get("phenomena") or []))
        or '<p class="muted">Sin fenómenos triangulados esta semana.</p>',
        f"<h2>Ranking ({len(series)})</h2>",
        '<div class="controls" id="rank-controls"><label>Últimos 12 meses '
        '<select data-role="state"><option value="">todos</option>'
        + concat(*(f'<option value="{esc(x)}">{esc(x)}</option>' for x in STATE_OPTIONS))
        + '</select></label><label>Ordenar <select data-role="sort">'
        '<option value="rank">por score</option><option value="state">por estado</option>'
        "</select></label></div>",
        '<div id="rank-list">'
        + (concat(*(_candidate(c) for c in series)) if series else none)
        + "</div>",
        f"<h2>🟡 En observación ({len(data.get('watchlist') or [])})</h2>",
        '<p class="lead watch-lead">Lista separada del ranking: fenómenos de <strong>un solo '
        "origen</strong> que pasan FDR y tienen evidencia de demanda (cantidad o valor). No están "
        "triangulados, no entran al score y no son candidatos del ranking; sirven para vigilarlos "
        "hasta que aparezca un segundo origen.</p>",
        concat(*(_watch_card(p) for p in data.get("watchlist") or []))
        or '<p class="muted">Sin fenómenos en observación esta semana.</p>',
        f"<h2>Candidatos solo de texto ({len(text)})</h2>",
        '<p class="muted">Términos emergentes parecidos en EDGAR y en el Federal Register.</p>',
        concat(*(_candidate(c) for c in text)) if text else none,
    )
    return page("Ranking semanal", "index.html", body, data["generated"])


def dashboard_page(data: dict[str, Any]) -> str:
    st, fun, cov = data["status"], data["funnel"], data["coverage"]
    runs = [
        [k, v["status"], ts(v["started_at"]), ts(v["finished_at"]), fnum(v["total_seconds"], 0)]
        for k, v in st["runs"].items()
    ]
    by_src: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in fun:
        by_src[r["source"]][r["group"]] += r["n"]
    groups = sorted({g for v in by_src.values() for g in v})
    funnel_rows = [
        [src_name(s), *[fnum(by_src[s].get(g, 0)) for g in groups], fnum(sum(by_src[s].values()))]
        for s in sorted(by_src)
    ]
    rules = [
        [src_name(r["source"]), r["rule"], fnum(r["n"])] for r in fun if r["group"] == "excluida"
    ]
    det_rows = [
        [
            d["family"],
            d["detector"],
            d["run_id"],
            ts(d["finished_at"]),
            d["n_tests"],
            d["n_bh"],
            f"{d['bh_rate']:.2%}" + (" ⚠" if d["alert"] else ""),
            d["n_by"],
        ]
        for d in st["detectors"]
    ]
    num_rows = [
        [
            src_name(r["source"]),
            r["frequency"],
            fnum(r["n_series"]),
            fnum(r["n_obs"]),
            r["start"],
            r["end"],
        ]
        for r in cov["numeric"]
    ]
    vint_rows = [[src_name(r["source"]), fnum(r["n_series"])] for r in cov["vintages"]]
    text_rows = [
        [
            src_name(r["source"]),
            r["section"],
            r["n_periods"],
            r["start"],
            r["end"],
            fnum(r["n_docs"]),
        ]
        for r in cov["text"]
    ]
    options = concat(
        *(
            f'<option value="{esc(s)}">{esc(src_name(s))}</option>'
            for s in sorted(data["explorer"]["by_source"])
        )
    )
    explorer = concat(
        '<div id="explorer"><div class="controls">',
        '<input type="search" placeholder="Buscar por título o id…" aria-label="Buscar series">',
        f'<select aria-label="Fuente"><option value="">Todas las fuentes</option>{options}'
        "</select></div>",
        '<p class="muted status"></p>',
        _table(
            [],
            {5, 7},
            "Fuente",
            "Id",
            "Título",
            "Frec.",
            "Unidades",
            "q",
            "Detector",
            "Términos",
            "Candidato",
        ),
        "</div>",
    )
    body = concat(
        "<h2>Estado del sistema</h2>",
        _table(runs, {4}, "Corrida", "Estado", "Inicio", "Fin", "Segundos"),
        "<h2>Detectores (última corrida)</h2>",
        _table(
            det_rows,
            {4, 5, 7},
            "Familia",
            "Detector",
            "Corrida",
            "Fin",
            "Pruebas",
            "BH",
            "% BH",
            "BY",
        ),
        "<h2>Embudo de elegibilidad</h2>",
        '<p class="muted">Última decisión de cada serie del catálogo, por grupo.</p>',
        _table(funnel_rows, set(range(1, len(groups) + 2)), "Fuente", *groups, "Total"),
        "<details><summary>Exclusiones por regla</summary>",
        _table(rules, {2}, "Fuente", "Regla", "Series"),
        "</details>",
        "<h2>Cobertura</h2>",
        _table(
            num_rows, {2, 3}, "Fuente", "Frecuencia", "Series", "Observaciones", "Desde", "Hasta"
        ),
        "<h3>Vintages (ALFRED)</h3>",
        _table(vint_rows, {1}, "Fuente", "Series con vintages"),
        "<h3>Texto</h3>",
        _table(text_rows, {2, 5}, "Fuente", "Sección", "Periodos", "Desde", "Hasta", "Documentos"),
        "<h2>Explorador de series</h2>",
        f'<p class="muted">{fnum(data["explorer"]["n_cards"])} series de las fuentes numéricas '
        "(sin las que tienen copyright). q = menor q de la última corrida de detectores.</p>",
        explorer,
    )
    return page("Dashboard", "dashboard.html", body, data["generated"])


def archive_page(entries: list[dict[str, Any]], generated: str) -> str:
    links = concat(
        *(
            f"<tr><td>{esc(e['date'])}</td><td>{esc(e['run_id'])}</td>"
            f"<td class=num>{fnum(e['n_published'])}</td>"
            f'<td><a href="archivo/{esc(e["date"])}/index.html">ver</a></td></tr>'
            for e in entries
        )
    )
    body = (
        "<h2>Archivo de reportes</h2>"
        '<div class="table-wrap"><table><thead><tr><th>Fecha</th><th>Corrida</th>'
        f"<th class=num>Publicados</th><th>Reporte</th></tr></thead><tbody>{links}</tbody>"
        "</table></div>"
    )
    return page("Archivo", "archivo.html", body, generated)
