"""Páginas HTML del sitio: ranking semanal, dashboard y archivo de reportes."""

from __future__ import annotations

import html
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
<dt>Unidades</dt><dd>{esc(s["units"])} ({esc(s["frequency"])}, {esc(s["seasonal_adjustment"])})</dd>
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
    return f"""<section class="card cand" id="{esc(c["candidate_id"])}" data-rank="{c["rank"]}"
 data-state="{esc(state)}">
<header><span class="rank">#{c["rank"]}</span>{badge}
<strong>{c["n_sources"]} fuentes independientes</strong>
<span>score {fnum(c["score"], 3)}</span>
<span class="muted">{c["n_members"]} miembro(s) · {len(c["terms"])} término(s)</span></header>
<div>{_sources_chips(c["sources"])}</div>
<p class="plain">{esc(_why_sources(c["sources"]))}</p>
<details><summary>Componentes del score</summary><p class="muted">{esc(comp)}</p></details>
{hidden}{ev}
</section>"""


def _tile(k: str, v: Any) -> str:
    return f'<div class="tile"><div class="v">{esc(v)}</div><div class="k">{esc(k)}</div></div>'


def ranking_page(data: dict[str, Any]) -> str:
    rr = data["rank_run"]
    det = data["status"]["detectors"]
    tiles = concat(
        '<div class="tiles">',
        _tile("Series con señal", fnum(rr.get("n_signal_series"))),
        _tile("Términos con señal", fnum(rr.get("n_signal_terms"))),
        _tile("Clusters", fnum(rr.get("n_clusters"))),
        _tile("Con ≥ 2 fuentes", fnum(rr.get("n_candidates"))),
        _tile("Publicados", fnum(rr.get("n_published"))),
        _tile("Una sola fuente (descartados)", fnum(rr.get("n_single_source"))),
        "</div>",
    )
    det_rows = [
        [
            d["family"],
            DETECTOR_NAMES.get(d["detector"], d["detector"]),
            d["n_tests"],
            d["n_bh"],
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
        _table(det_rows, {2, 3, 4, 5}, "Familia", "Detector", "Pruebas", "BH", "BY", "q"),
        '<p class="muted">BH decide; BY se reporta solo como comparación.</p>',
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
        _table(det_rows, {4, 5, 6}, "Familia", "Detector", "Corrida", "Fin", "Pruebas", "BH", "BY"),
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
