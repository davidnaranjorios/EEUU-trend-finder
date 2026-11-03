"""Páginas HTML del sitio: ranking semanal, dashboard y archivo de reportes."""

from __future__ import annotations

import html
import math
from collections import defaultdict
from typing import Any

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
    return "".join(f'<span class="chip">{esc(" + ".join(g))}</span>' for g in groups)


def _series_block(s: dict[str, Any]) -> str:
    dets = ", ".join(DETECTOR_NAMES.get(d, d) for d in s["detectors"])
    w = s["window"]
    return f"""<div class="ev">
<div><h3>{esc(s["title"])}</h3>
<dl><dt>Fuente</dt><dd>{esc(src_name(s["source"]))} · {esc(s["id"])}</dd>
<dt>Agencia</dt><dd>{esc(", ".join(s["agencies"]) or "—")}</dd>
<dt>Unidades</dt><dd>{esc(s["units"])} ({esc(s["frequency"])}, {esc(s["seasonal_adjustment"])})</dd>
<dt>Detector</dt><dd>{esc(dets)}</dd>
<dt>q (BH)</dt><dd>{fq(s["q_value"])}</dd>
<dt>Efecto</dt><dd>{fnum(s["effect_size"])} sd</dd>
<dt>Ventana</dt><dd>{esc(w[0])} → {esc(w[1])}</dd></dl></div>
<div>{s["chart"]}</div>
</div>"""


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
        link = f"<dt>Similitud</dt><dd>{fnum(t['link_score'], 3)} con {target}</dd>"
    ent = f"<dt>Entidades</dt><dd>{fnum(t['n_entities'])}</dd>" if t["n_entities"] else ""
    return f"""<div class="ev">
<div><h3>“{esc(t["term"])}”</h3>
<dl><dt>Fuente</dt><dd>{esc(src_name(t["source"]))} · sección {esc(t["section"])}</dd>
<dt>q (BH)</dt><dd>{fq(t["q_value"])}</dd>
<dt>Efecto</dt><dd>{fnum(t["effect_size"])} logit</dd>{ent}{link}
<dt>Ventana</dt><dd>{esc(w[0])} → {esc(w[1])}</dd></dl></div>
<div>{t["chart"]}</div>
</div>"""


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
    return f"""<section class="card" id="{esc(c["candidate_id"])}">
<header><span class="rank">#{c["rank"]}</span>
<strong>{c["n_sources"]} fuentes independientes</strong>
<span>score {fnum(c["score"], 3)}</span>
<span class="muted">{c["n_members"]} miembro(s) · {len(c["terms"])} término(s)</span></header>
<div>{_sources_chips(c["sources"])}</div>
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
        "<h2>Señales por detector</h2>",
        _table(det_rows, {2, 3, 4, 5}, "Familia", "Detector", "Pruebas", "BH", "BY", "q"),
        '<p class="muted">BH decide; BY se reporta solo como comparación.</p>',
        f"<h2>Ranking ({len(series)})</h2>",
        concat(*(_candidate(c) for c in series)) if series else none,
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
