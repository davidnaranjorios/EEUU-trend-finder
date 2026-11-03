"""Gráficos SVG en línea para el reporte (sin dependencias ni CDN).

Una serie por gráfico (el título de la ficha la nombra; no hace falta leyenda). Línea de 2 px en
el color de la serie, ejes y grilla recesivos, ventana de la señal sombreada en gris neutro y,
si hay, una marca vertical en el inicio del régimen. Los puntos van en `data-points` para el
tooltip de `site.js` (cruz + valor al pasar el cursor).
"""

from __future__ import annotations

import html
import json
import math
from collections.abc import Callable, Sequence
from datetime import date

WIDTH, HEIGHT = 560, 180
PAD_L, PAD_R, PAD_T, PAD_B = 48, 12, 10, 24


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return [lo]
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=raw)
    start = math.ceil(lo / step) * step
    return [start + i * step for i in range(int((hi - start) / step) + 1)]


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 1:
        return f"{v:.2f}"
    return f"{v:.3g}"


def line_chart(
    dates: Sequence[date],
    values: Sequence[float],
    *,
    label: str,
    window: tuple[date, date] | None = None,
    marker: date | None = None,
    log_scale: bool = False,
    value_format: str = "number",
) -> str:
    """SVG de una serie; `log_scale` dibuja log(valor) con etiquetas en el valor original."""
    pts = [(d, v) for d, v in zip(dates, values, strict=True) if v is not None and math.isfinite(v)]
    if log_scale:
        pts = [(d, v) for d, v in pts if v > 0]
    if len(pts) < 2:
        return '<p class="muted">Sin datos suficientes para el gráfico.</p>'
    xs = [d.toordinal() for d, _ in pts]
    ys = [math.log(v) if log_scale else v for _, v in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if y1 == y0:
        y0, y1 = y0 - 1, y1 + 1
    pad = (y1 - y0) * 0.06
    y0, y1 = y0 - pad, y1 + pad
    iw, ih = WIDTH - PAD_L - PAD_R, HEIGHT - PAD_T - PAD_B

    def sx(x: float) -> float:
        return PAD_L + (x - x0) / max(x1 - x0, 1) * iw

    def sy(y: float) -> float:
        return PAD_T + (1 - (y - y0) / (y1 - y0)) * ih

    parts = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="{html.escape(label)}" preserveAspectRatio="xMidYMid meet" '
        f'data-points="{html.escape(_points_json(pts, xs, ys, sx, sy, value_format))}">'
    ]
    if window:
        wa, wb = max(window[0].toordinal(), x0), min(window[1].toordinal(), x1)
        if wb > wa:
            parts.append(
                f'<rect class="win" x="{sx(wa):.1f}" y="{PAD_T}" width="{sx(wb) - sx(wa):.1f}" '
                f'height="{ih}"><title>Ventana de la señal</title></rect>'
            )
    # Grilla y eje y.
    if log_scale:
        lo, hi = math.exp(y0), math.exp(y1)
        ticks = [t for t in _nice_ticks(lo, hi) if t > 0]
        tick_pos = [(math.log(t), t) for t in ticks]
    else:
        tick_pos = [(t, t) for t in _nice_ticks(y0, y1)]
    for y, t in tick_pos:
        if y0 <= y <= y1:
            parts.append(
                f'<line class="grid" x1="{PAD_L}" x2="{WIDTH - PAD_R}" y1="{sy(y):.1f}" '
                f'y2="{sy(y):.1f}"/><text class="tick" x="{PAD_L - 6}" y="{sy(y) + 3:.1f}" '
                f'text-anchor="end">{_fmt_tick(t, value_format)}</text>'
            )
    # Eje x: años.
    ya, yb = date.fromordinal(x0).year, date.fromordinal(x1).year
    step = max(1, math.ceil((yb - ya + 1) / 6))
    for yr in range(ya + 1, yb + 1, step):
        x = date(yr, 1, 1).toordinal()
        if x0 <= x <= x1:
            parts.append(
                f'<text class="tick" x="{sx(x):.1f}" y="{HEIGHT - 6}" '
                f'text-anchor="middle">{yr}</text>'
            )
    parts.append(
        f'<line class="axis" x1="{PAD_L}" x2="{WIDTH - PAD_R}" y1="{PAD_T + ih}" '
        f'y2="{PAD_T + ih}"/>'
    )
    if marker and x0 <= marker.toordinal() <= x1:
        mx = sx(marker.toordinal())
        parts.append(
            f'<line class="mark" x1="{mx:.1f}" x2="{mx:.1f}" y1="{PAD_T}" y2="{PAD_T + ih}">'
            f"<title>Inicio del régimen: {marker.isoformat()}</title></line>"
        )
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}"
        for i, (x, y) in enumerate(zip(xs, ys, strict=True))
    )
    parts.append(f'<path class="line" d="{path}"/>')
    parts.append(
        f'<g class="hover" visibility="hidden"><line class="cross" y1="{PAD_T}" '
        f'y2="{PAD_T + ih}"/><circle class="dot" r="4"/></g>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _fmt_tick(t: float, value_format: str) -> str:
    return f"{t * 100:.2g}%" if value_format == "share" else _fmt(t)


def _points_json(
    pts: list[tuple[date, float]],
    xs: list[int],
    ys: list[float],
    sx: Callable[[float], float],
    sy: Callable[[float], float],
    value_format: str,
) -> str:
    """[[x_px, y_px, fecha, valor], ...] más el formato, para el tooltip de site.js."""
    return json.dumps(
        {
            "f": value_format,
            "p": [
                [round(sx(x), 1), round(sy(y), 1), d.isoformat(), round(v, 8)]
                for (d, v), x, y in zip(pts, xs, ys, strict=True)
            ],
        },
        separators=(",", ":"),
    )
