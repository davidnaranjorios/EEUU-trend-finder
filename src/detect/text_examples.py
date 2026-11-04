"""Entrega D: empresas de ejemplo de los términos emergentes de EDGAR (revisión 2a02c981).

Uso: `uv run python -m src.detect.text_examples --report data/logs/detector_texto_edgar.json`

Toma los `top_priority` (los 20 términos de mayor efecto tras los filtros) del reporte del detector
y busca, en una muestra de los 10-K del periodo reciente (el último de `text_docs`), tres empresas
(nombre + CIK) cuyo documento contiene el término en la sección donde se detectó. El texto de los
documentos no se guarda (ADR 0005): se vuelve a bajar de EDGAR y se descarta. Imprime Markdown
(el workflow Detectores lo escribe en el resumen del job).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from src.config import REPO_ROOT, load_config
from src.ingest.edgar import (
    EdgarSource,
    Filing,
    _nodash,
    extract_sections,
    html_to_text,
    looks_like_html,
)
from src.ingest.http import ApiError
from src.ingest.text_terms import iter_ngrams, normalize


def recent_filings(processed: Path, source: str) -> tuple[str, list[Filing]]:
    """(periodo, 10-K) del último periodo con documentos en `text_docs`."""
    files = sorted((processed / "text_docs" / source).glob("*/*.parquet"))
    if not files:
        raise RuntimeError(f"no hay text_docs de {source}")
    period = max(f.parent.name for f in files)
    latest = [str(f) for f in files if f.parent.name == period]
    rows = (
        duckdb.connect()
        .execute(
            "SELECT DISTINCT doc_id, entity, date FROM read_parquet(?) WHERE entity IS NOT NULL "
            "ORDER BY doc_id",
            [latest],
        )
        .fetchall()
    )
    return period, [
        Filing(doc_id, "10-K", cik, d if isinstance(d, date) else date.fromisoformat(str(d)), "")
        for doc_id, cik, d in rows
    ]


def find_examples(
    src: EdgarSource,
    filings: list[Filing],
    wanted: list[str],
    *,
    per_term: int = 3,
    sample: int = 400,
    seed: int = 20261104,
    n_max: int = 3,
) -> dict[str, list[dict[str, str]]]:
    """`wanted`: object_id 'sección|término'. Devuelve object_id -> hasta `per_term` empresas."""
    out: dict[str, list[dict[str, str]]] = {o: [] for o in wanted}
    picks = sorted(
        random.Random(seed).sample(filings, min(sample, len(filings))), key=lambda f: f.accession
    )
    primary = src.primary_documents(picks)
    for f in picks:
        if all(len(v) >= per_term for v in out.values()):
            break
        name = primary.get(f.accession)
        if not name:
            continue
        url = f"/Archives/edgar/data/{int(f.cik)}/{_nodash(f.accession)}/{name}"
        try:
            text = src.www.get_bytes(url).decode("utf-8", errors="replace")
        except ApiError:
            continue
        if looks_like_html(text):
            text = html_to_text(text.encode("utf-8"))
        grams = {
            section: set(iter_ngrams(normalize(raw), n_max))
            for section, raw in extract_sections(text).items()
        }
        for oid in wanted:
            section, term = oid.split("|", 1)
            if len(out[oid]) < per_term and term in grams.get(section, ()):
                out[oid].append(
                    {
                        "company": src.names.get(int(f.cik), "(sin nombre)"),
                        "cik": f.cik,
                        "accession": f.accession,
                    }
                )
    return out


def to_markdown(
    top: list[dict[str, Any]], examples: dict[str, list[dict[str, str]]], period: str
) -> str:
    lines = [
        f"## Entrega D — términos emergentes de EDGAR (ejemplos del periodo {period})",
        "",
        "| # | Término | Efecto | Prioridad | q (BH) | SIC2 | Ventana | Empresas (CIK) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, t in enumerate(top, start=1):
        sec, term = t["object_id"].split("|", 1)
        ex = "; ".join(f"{e['company']} ({e['cik']})" for e in examples.get(t["object_id"], []))
        ind = "—" if t.get("n_industries") is None else f"{t['n_industries']:.0f}"
        lines.append(
            f"| {i} | {term} ({sec}) | {t['effect_size']} | {t['priority']} | "
            f"{t['q_value']:.2g} | {ind} | {t['window_start']} → {t['window_end']} | "
            f"{ex or 'sin ejemplo en la muestra'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.detect.text_examples")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--per-term", type=int, default=3)
    parser.add_argument("--sample", type=int, default=400)
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    from src.ingest.text import build_source

    load_dotenv(REPO_ROOT / ".env")
    cfg = load_config()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    top = report["top_priority"]
    processed = Path(cfg["paths"]["processed_dir"])
    processed = processed if processed.is_absolute() else REPO_ROOT / processed
    period, filings = recent_filings(processed, report["source"])
    src = build_source("edgar", cfg, REPO_ROOT / cfg["paths"]["raw_dir"])
    try:
        examples = find_examples(
            src,  # type: ignore[arg-type]
            filings,
            [t["object_id"] for t in top],
            per_term=args.per_term,
            sample=args.sample,
            n_max=cfg["text"]["ngram_max"],
        )
    finally:
        src.close()
    print(to_markdown(top, examples, period))
    return 0


if __name__ == "__main__":
    sys.exit(main())
