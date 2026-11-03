"""Exportes pequeños y preagregados para una futura web estática (ADR 0002 y 0004).

Salida en `data/exports/` (se sincroniza a R2 con el resto de data/):
- `funnel.json`: el embudo de la última corrida de elegibilidad (todos los grupos).
- `series/<NN>.json`: fichas de serie solo con metadatos, de los grupos de `exports.groups`,
  repartidas en `exports.n_buckets` buckets por id. Formato columnar (`columns` + `rows`) para
  no repetir las claves en cada ficha.
- `categories.json`: diccionario de rutas de categoría; las fichas guardan índices.
- `series/index.json`: conteos y tamaño total.

Tope de tamaño (`exports.max_file_kb`, `exports.max_total_mb`): se calcula todo en memoria y,
si algo lo pasa, falla sin escribir nada (`ExportTooLarge`).
Las series con copyright de terceros no se exportan (términos de FRED): solo se cuentan.
El grafo de cruces serie-término (`crossings_graph.json`) se añade en la Fase 5.

Uso: `uv run python -m src.export`.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.config import load_config
from src.eligibility import funnel, latest_decisions, load_inputs
from src.ingest.fred import Paths, _resolve, paths_from_config
from src.ingest.store import bucket_of


class ExportTooLarge(RuntimeError):
    """Un exporte pasa el tope de tamaño de config.yaml."""


def export_all(
    paths: Paths, out_dir: Path, exports_cfg: dict[str, Any], v2_page_limit: int
) -> dict[str, Any]:
    groups = set(exports_cfg["groups"])
    n_buckets = exports_cfg["n_buckets"]
    decisions = latest_decisions(paths)
    catalog, attrs = load_inputs(paths)
    run_id = decisions[0]["run_id"]
    files: dict[str, bytes] = {
        "funnel.json": _dumps(
            {"run_id": run_id, **funnel(decisions, catalog, attrs, v2_page_limit)}
        )
    }

    cat = {r["series_id"]: r for r in catalog}
    category_index: dict[str, int] = {}
    buckets: dict[int, list[list[Any]]] = defaultdict(list)
    columns: list[str] = []
    withheld = 0
    for d in sorted(decisions, key=lambda d: d["series_id"]):
        if d["group_name"] not in groups:
            continue
        if d["copyrighted"]:
            withheld += 1
            continue
        c = cat[d["series_id"]]
        paths_ = [p for p in (c["category_path"] or "").split(";") if p]
        card = {
            "id": d["series_id"],
            "title": c["title"],
            "group": d["group_name"],
            "geo_level": d["geo_level"],
            "frequency": d["frequency"],
            "units": c["units"],
            "seasonal_adjustment": c["seasonal_adjustment"],
            "start": c["observation_start"],
            "end": c["observation_end"],
            "last_updated": c["last_updated"].date() if c["last_updated"] else None,
            "categories": [category_index.setdefault(p, len(category_index)) for p in paths_],
            "rule": d["rule"],
            "detail": d["detail"],
        }
        columns = columns or list(card)
        buckets[bucket_of(d["series_id"], n_buckets)].append(list(card.values()))
    for b, rows in sorted(buckets.items()):
        files[f"series/{b:02d}.json"] = _dumps({"run_id": run_id, "columns": columns, "rows": rows})
    files["categories.json"] = _dumps({"run_id": run_id, "paths": list(category_index)})
    index: dict[str, Any] = {
        "run_id": run_id,
        "groups": sorted(groups),
        "n_buckets": n_buckets,
        "cards": sum(len(v) for v in buckets.values()),
        "copyrighted_withheld": withheld,
    }
    index["files"] = len(files) + 1
    index["total_bytes"] = sum(len(v) for v in files.values())
    index["max_file_bytes"] = max(len(v) for v in files.values())
    files["series/index.json"] = _dumps(index)
    index["total_bytes"] += len(files["series/index.json"])

    check_size(files, exports_cfg)
    _write_all(out_dir, files)
    return index


def check_size(files: dict[str, bytes], exports_cfg: dict[str, Any]) -> None:
    """Lanza ExportTooLarge si un archivo o el total pasa el tope."""
    max_file = exports_cfg["max_file_kb"] * 1024
    max_total = exports_cfg["max_total_mb"] * 1024 * 1024
    big = {name: len(data) for name, data in files.items() if len(data) > max_file}
    if big:
        worst = max(big, key=big.get)
        raise ExportTooLarge(
            f"{len(big)} exportes pasan exports.max_file_kb={exports_cfg['max_file_kb']} "
            f"(el mayor: {worst}, {big[worst] / 1024:.0f} KB)"
        )
    total = sum(len(v) for v in files.values())
    if total > max_total:
        raise ExportTooLarge(
            f"los exportes suman {total / 1024 / 1024:.1f} MB, más que "
            f"exports.max_total_mb={exports_cfg['max_total_mb']}"
        )


def _write_all(out_dir: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    # Fichas de buckets que ya no existen (p. ej., tras cambiar exports.n_buckets).
    for stale in (out_dir / "series").glob("*.json"):
        if f"series/{stale.name}" not in files:
            stale.unlink()


def _dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=_default).encode(
        "utf-8"
    )


def _default(value: Any) -> str:
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"no serializable: {type(value).__name__}")


def main() -> int:
    cfg = load_config()
    out = _resolve(cfg["paths"]["data_dir"]) / "exports"
    try:
        stats = export_all(
            paths_from_config(cfg), out, cfg["exports"], cfg["download"]["v2_page_limit"]
        )
    except ExportTooLarge as exc:
        print(f"export: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
