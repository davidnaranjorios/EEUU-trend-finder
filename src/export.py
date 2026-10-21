"""Exportes pequeños y preagregados para una futura web estática (ADR 0002).

Salida en `data/exports/` (se sincroniza a R2 con el resto de data/):
- `funnel.json`: el embudo de la última corrida de elegibilidad.
- `series/<NN>.json`: fichas de serie solo con metadatos, repartidas en buckets por id.
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
from src.ingest.fred import SOURCE, Paths, _resolve, paths_from_config
from src.ingest.store import bucket_of

# Grupos que llegan a las fichas: los que entran al análisis.
CARD_GROUPS = ("nacional", "regional", "internacional", "nuevas")


def export_all(paths: Paths, out_dir: Path, n_buckets: int, v2_page_limit: int) -> dict[str, Any]:
    decisions = latest_decisions(paths)
    catalog, attrs = load_inputs(paths)
    run_id = decisions[0]["run_id"]
    summary = {"run_id": run_id, **funnel(decisions, catalog, attrs, v2_page_limit)}
    _write_json(out_dir / "funnel.json", summary)

    cat = {r["series_id"]: r for r in catalog}
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    withheld = 0
    for d in decisions:
        if d["group_name"] not in CARD_GROUPS:
            continue
        if d["copyrighted"]:
            withheld += 1
            continue
        c = cat[d["series_id"]]
        buckets[bucket_of(d["series_id"], n_buckets)].append(
            {
                "id": d["series_id"],
                "source": SOURCE,
                "title": c["title"],
                "group": d["group_name"],
                "geo_level": d["geo_level"],
                "frequency": d["frequency"],
                "units": c["units"],
                "seasonal_adjustment": c["seasonal_adjustment"],
                "start": c["observation_start"],
                "end": c["observation_end"],
                "last_updated": c["last_updated"],
                "categories": (c["category_path"] or "").split(";"),
                "rule": d["rule"],
                "detail": d["detail"],
            }
        )
    series_dir = out_dir / "series"
    for b, cards in buckets.items():
        cards.sort(key=lambda x: x["id"])
        _write_json(series_dir / f"{b:02d}.json", {"run_id": run_id, "series": cards})
    index = {
        "run_id": run_id,
        "n_buckets": n_buckets,
        "cards": sum(len(v) for v in buckets.values()),
        "copyrighted_withheld": withheld,
    }
    _write_json(out_dir / "series" / "index.json", index)
    return index


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=_default),
        encoding="utf-8",
    )
    tmp.replace(path)


def _default(value: Any) -> str:
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"no serializable: {type(value).__name__}")


def main() -> int:
    cfg = load_config()
    out = _resolve(cfg["paths"]["data_dir"]) / "exports"
    stats = export_all(
        paths_from_config(cfg), out, cfg["fred"]["n_buckets"], cfg["download"]["v2_page_limit"]
    )
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
