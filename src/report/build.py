"""Construye el sitio estático del reporte semanal (Fase 5, ADR 0009).

Uso: `uv run python -m src.report build [--rank-run ID] [--site-dir D]`

Salida:
- `report.site_dir` (por defecto `data/site/`): `index.html` (ranking), `dashboard.html`
  (estado, embudo, cobertura, explorador), `archivo.html`, `assets/`, `data/explorer/` y
  `archivo/<fecha>/index.html` (copia de cada reporte semanal, desde `data/reports/`).
- `data/reports/<fecha>/`: el reporte de la semana (`index.html`, `report.json`) y, aparte,
  `private_candidates.json` con los candidatos que no se publican (evidencia pública con una
  sola fuente). Se sincroniza a R2 (privado), nunca al sitio.

Antes de terminar, `src.report.guard.assert_publishable` recorre todo el sitio buscando ids y
títulos de series con copyright; si encuentra uno, falla y el paso de publicación no corre.
El destino de publicación (Cloudflare Pages, GitHub Pages o ninguno) es un paso aparte del
workflow: aquí solo se construye el directorio.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src.catalog import connect
from src.config import load_config
from src.detect.run import _resolve
from src.log import get_logger, run
from src.report import data as rdata
from src.report.explorer import explorer_files
from src.report.guard import assert_publishable, forbidden_sets
from src.report.render import archive_page, dashboard_page, ranking_page

STATIC = Path(__file__).parent / "static"


def build_site(
    cfg: dict[str, Any], *, rank_run_id: str | None = None, site_dir: Path | None = None
) -> dict[str, Any]:
    site = site_dir or _resolve(cfg["report"]["site_dir"])
    reports = _resolve(cfg["report"]["reports_dir"])
    con = connect(_resolve(cfg["paths"]["processed_dir"]))
    rank_run = rdata.load_rank_run(cfg, rank_run_id)
    generated = datetime.now(UTC).replace(microsecond=0).isoformat()

    copyrighted = rdata.copyrighted_series(con)
    forbidden_keys = {(s, i) for s, i, _ in copyrighted}
    candidates = {"series": [], "text": []}
    private = {"series": [], "text": []}
    if rank_run:
        candidates = rdata.candidates_data(con, cfg, rank_run, forbidden_keys)
        allc = rdata.candidates_data(con, cfg, rank_run, forbidden_keys, published_only=False)
        published_ids = {c["candidate_id"] for v in candidates.values() for c in v}
        private = _strip_charts(
            {k: [c for c in v if c["candidate_id"] not in published_ids] for k, v in allc.items()}
        )
    published_series = {
        (s["source"], s["id"]): c["candidate_id"]
        for v in candidates.values()
        for c in v
        for s in c["series"]
    }
    explorer = explorer_files(
        con,
        cfg,
        numeric_run=(rank_run or {}).get("numeric_run_id"),
        rank_run=(rank_run or {}).get("run_id"),
        published=published_series,
        forbidden=forbidden_keys,
    )
    explorer_index = json.loads(explorer["data/explorer/index.json"])
    data = {
        "generated": generated,
        "rank_run": {k: v for k, v in (rank_run or {}).items() if k != "params_json"},
        "status": rdata.system_status(cfg, rank_run),
        "funnel": rdata.funnel(con),
        "coverage": rdata.coverage(con),
        "candidates": candidates,
        "explorer": explorer_index,
    }
    public_ids = con.execute(
        "SELECT DISTINCT m.series_id, m.title FROM series_meta m WHERE NOT m.copyrighted"
    ).fetchall()
    con.close()

    # Escritura: directorio temporal y reemplazo, para no dejar un sitio a medias.
    tmp = site.with_name(site.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "assets").mkdir(parents=True)
    for f in STATIC.iterdir():
        shutil.copy2(f, tmp / "assets" / f.name)
    for name, blob in explorer.items():
        p = tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    ranking = ranking_page(data)
    (tmp / "index.html").write_text(ranking, "utf-8")
    (tmp / "dashboard.html").write_text(dashboard_page(data), "utf-8")

    today = date.today().isoformat()
    week = reports / today
    week.mkdir(parents=True, exist_ok=True)
    (week / "index.html").write_text(relocate(ranking, "../../"), "utf-8")
    (week / "report.json").write_text(_dumps(_strip_charts(data)), "utf-8")
    (week / "meta.json").write_text(
        _dumps(
            {
                "date": today,
                "run_id": (rank_run or {}).get("run_id"),
                "n_published": sum(len(v) for v in candidates.values()),
            }
        ),
        "utf-8",
    )
    (week / "private_candidates.json").write_text(_dumps(private), "utf-8")
    entries = []
    for meta in sorted(reports.glob("*/meta.json"), reverse=True):
        m = json.loads(meta.read_text("utf-8"))
        entries.append(m)
        dst = tmp / "archivo" / m["date"]
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(meta.parent / "index.html", dst / "index.html")
        shutil.copy2(meta.parent / "report.json", dst / "report.json")
    (tmp / "archivo.html").write_text(archive_page(entries, generated), "utf-8")

    ids, titles = forbidden_sets(
        ((i, t) for _, i, t in copyrighted), ((i, t) for i, t in public_ids)
    )
    assert_publishable(tmp, ids, titles)
    shutil.rmtree(site, ignore_errors=True)
    tmp.replace(site)
    return {
        "site_dir": str(site),
        "rank_run": (rank_run or {}).get("run_id"),
        "n_published": sum(len(v) for v in candidates.values()),
        "n_private": sum(len(v) for v in private.values()),
        "explorer_cards": explorer_index["n_cards"],
        "copyrighted_forbidden": len(ids),
        "files": sum(1 for p in site.rglob("*") if p.is_file()),
        "bytes": sum(p.stat().st_size for p in site.rglob("*") if p.is_file()),
    }


def relocate(page: str, prefix: str) -> str:
    """Rutas relativas de una página copiada a `archivo/<fecha>/`."""
    for attr in ('href="', 'src="'):
        for target in ("assets/", "index.html", "dashboard.html", "archivo.html"):
            page = page.replace(attr + target, attr + prefix + target)
    return page


def _strip_charts(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_charts(v) for k, v in obj.items() if k != "chart"}
    if isinstance(obj, list):
        return [_strip_charts(v) for v in obj]
    return obj


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.report")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="construye el sitio y verifica que no tenga copyright")
    b.add_argument("--rank-run", default=None)
    b.add_argument("--site-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    cfg = load_config()
    log = get_logger("report", cfg["logging"]["level"])
    with run(["report"], _resolve(cfg["report"]["runs_file"])) as record:
        with record.stage("build"):
            out = build_site(cfg, rank_run_id=args.rank_run, site_dir=args.site_dir)
        record.details = {"command": "report build", **out}
        log.info("sitio construido", extra={"fields": out})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
