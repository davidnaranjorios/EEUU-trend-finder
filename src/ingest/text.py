"""Ingesta de texto por periodos reanudables: EDGAR (10-K) y Federal Register (ADR 0005).

Uso:
    uv run python -m src.ingest.text edgar [--max-periods N] [--periods P,...] [--sample N]
    uv run python -m src.ingest.text federal_register [...]
    uv run python -m src.ingest.text summary

Un tramo es un periodo de una fuente (trimestre de presentación en EDGAR, mes en el Federal
Register). Cada tramo se procesa completo y reemplaza su partición de `text_docs`,
`term_freq` y `lineage/text_ingest`; por eso repetirlo no duplica nada. Los periodos cerrados
quedan marcados en `ingest_state/text/<fuente>.json`; los abiertos se reprocesan en cada
corrida. Código de salida 10: quedan periodos pendientes (como `src.ingest.fred`).
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pyarrow as pa

from src.config import REPO_ROOT, load_config
from src.ingest import store
from src.ingest.text_terms import (
    PeriodCounter,
    ngram_hashes,
    normalize,
    read_temp_text,
    resolve_strings,
    text_hash,
    write_temp_text,
)
from src.log import get_logger, run
from src.schemas import TERM_FREQ, TEXT_DOCS, TEXT_INGEST, TEXT_STRUCTURE

EXIT_PENDING = 10
EXIT_INCOMPLETE = 11
EXIT_BUDGET = 12  # se agotó el presupuesto de tiempo de la corrida (ADR 0026): quedan periodos
# Versión del esquema de las particiones que escribe la ingesta (ADR 0008):
#   2: term_freq.n_entities;  3: entidad del Federal Register normalizada;
#   4: term_freq.n_industries (SIC de EDGAR);  5: term_freq.sic_top_json (SIC de 4 dígitos);
#   6: tabla `text_structure` (ADR 0026).
# Un periodo cerrado con versión menor que `<fuente>.reprocess_below_schema` se vuelve a
# procesar; las particiones viejas que no se reprocesan llevan n_entities = NULL.
SCHEMA_VERSION = 6
DOC_LEVEL = "*"  # `section` de las decisiones sobre el documento entero
TABLE_DIRS = {
    TEXT_DOCS.name: "text_docs",
    TERM_FREQ.name: "term_freq",
    TEXT_INGEST.name: "lineage/text_ingest",
    TEXT_STRUCTURE.name: "text_structure",
}


# --- Resultados por documento ------------------------------------------------------------------


@dataclass
class SectionResult:
    section: str
    status: str
    detail: str | None = None
    n_chars: int | None = None
    text_hash: str | None = None
    hashes: np.ndarray | None = None
    tmp_path: str | None = None


@dataclass
class DocResult:
    doc_id: str
    date: date
    entity: str | None
    sections: list[SectionResult] = field(default_factory=list)
    group: str | None = None  # industria (SIC de 2 dígitos) cuando la fuente la tiene
    sic: str | None = None  # SIC de 4 dígitos (para la concordancia oficial SIC → NAICS)
    structure: dict[str, Any] | None = None  # estructura del documento (ADR 0026): solo describe


def analyze_section(
    section: str, raw_text: str, *, min_chars: int, n_max: int, tmp_path: Path
) -> SectionResult:
    """Normaliza, mide y extrae los hashes de n-gramas de una sección (corre en los workers)."""
    norm = normalize(raw_text)
    if len(norm) < min_chars:
        return SectionResult(section, "muy_corta", f"min_chars={min_chars}", len(norm))
    write_temp_text(tmp_path, norm)
    return SectionResult(
        section,
        "ok",
        None,
        len(norm),
        text_hash(norm),
        ngram_hashes(norm, n_max),
        str(tmp_path),
    )


# --- Fuentes -----------------------------------------------------------------------------------


class TextSource(Protocol):
    """Lo que cada fuente de texto implementa (ver edgar.py y federal_register.py)."""

    name: str

    def periods(self, today: date) -> list[str]:
        """Periodos desde el inicio configurado hasta el actual, en orden."""
        ...

    def is_closed(self, period: str, today: date) -> bool:
        """Si el periodo ya no puede recibir documentos nuevos."""
        ...

    def collect(self, period: str, ctx: CollectContext) -> list[DocResult]:
        """Descarga y analiza los documentos del periodo."""
        ...

    def close(self) -> None: ...


@dataclass
class CollectContext:
    """Lo que una fuente necesita para procesar un periodo."""

    tmp_dir: Path
    executor: Executor | None
    min_chars: int
    n_max: int
    sample: int | None = None
    seed: int = 0
    max_in_flight: int = 16

    def run_tasks(self, fn: Callable[..., Any], tasks: Iterable[tuple]) -> Iterator[Any]:
        """Aplica `fn(*task)` en los workers con un tope de tareas en vuelo; en orden."""
        if self.executor is None:
            for task in tasks:
                yield fn(*task)
            return
        pending: list[Future] = []
        for task in tasks:
            pending.append(self.executor.submit(fn, *task))
            while len(pending) >= self.max_in_flight:
                yield pending.pop(0).result()
        for fut in pending:
            yield fut.result()


# --- Escritura de un periodo -------------------------------------------------------------------


def finalize_period(
    source: str,
    period: str,
    docs: list[DocResult],
    *,
    processed_dir: Path,
    run_id: str,
    min_doc_count: int,
    top_sic: int,
    n_max: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deduplica, cuenta y reemplaza las tres particiones del periodo. Determinista."""
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    docs = sorted(docs, key=lambda d: d.doc_id)
    seen_doc: set[str] = set()
    seen_hash: dict[tuple[str, str], str] = {}
    counter = PeriodCounter()
    tmp_by_section: dict[str, list[str]] = {}
    text_rows: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    structure_rows: list[dict[str, Any]] = []

    for doc in docs:
        if doc.doc_id in seen_doc:  # mismo documento listado dos veces
            continue
        seen_doc.add(doc.doc_id)
        if doc.structure is not None:
            structure_rows.append(structure_row(source, period, doc))
        for sec in doc.sections:
            status, detail = sec.status, sec.detail
            if status == "ok":
                key = (sec.section, sec.text_hash or "")
                if key in seen_hash:
                    status, detail = "duplicado", f"duplicado_de={seen_hash[key]}"
                else:
                    seen_hash[key] = doc.doc_id
                    counter.add(sec.section, sec.hashes, doc.entity, doc.group, doc.sic)
                    tmp_by_section.setdefault(sec.section, []).append(sec.tmp_path)
                    text_rows.append(
                        {
                            "source": source,
                            "doc_id": doc.doc_id,
                            "date": doc.date,
                            "entity": doc.entity,
                            "section": sec.section,
                            "text_hash": sec.text_hash,
                        }
                    )
            lineage.append(
                {
                    "run_id": run_id,
                    "source": source,
                    "doc_id": doc.doc_id,
                    "section": sec.section,
                    "period": period,
                    "status": status,
                    "detail": detail,
                    "n_chars": sec.n_chars,
                    "decided_at": now,
                }
            )

    freq_rows: list[dict[str, Any]] = []
    for section in counter.sections():
        hashes, counts = counter.retained(section, min_doc_count)
        entities = counter.n_entities(section, hashes)
        industries = counter.n_groups(section, hashes)
        sic_top = counter.sic_top(section, hashes, top_sic)
        texts = (read_temp_text(Path(p)) for p in tmp_by_section[section])
        strings = resolve_strings(hashes, texts, n_max)
        total = counter.total_docs[section]
        ind = industries.tolist() if industries is not None else [None] * len(hashes)
        for h, c, e, n_ind, st in zip(
            hashes.tolist(), counts.tolist(), entities.tolist(), ind, sic_top, strict=True
        ):
            freq_rows.append(
                {
                    "source": source,
                    "section": section,
                    "period": period,
                    "term": strings[h],
                    "doc_count": c,
                    "total_docs": total,
                    "n_entities": e,
                    "n_industries": n_ind,
                    "sic_top_json": st,
                }
            )

    written = {}
    for spec, rows in (
        (TEXT_DOCS, text_rows),
        (TEXT_INGEST, lineage),
        (TERM_FREQ, freq_rows),
        (TEXT_STRUCTURE, structure_rows),
    ):
        table = pa.Table.from_pylist(rows, schema=spec.schema)
        written[spec.name] = store.replace_partition(
            table, spec, processed_dir / TABLE_DIRS[spec.name], source=source, partition=period
        )
    by_status: dict[str, int] = {}
    for row in lineage:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    return {
        "period": period,
        "docs": len(seen_doc),
        "rows": written,
        "status": by_status,
        "total_docs": dict(counter.total_docs),
    }


def structure_row(source: str, period: str, doc: DocResult) -> dict[str, Any]:
    """Fila de `text_structure` de un documento (campos ausentes: NULL)."""
    st = doc.structure or {}
    items = st.get("items")
    cfr, topics = st.get("cfr"), st.get("topics")
    return {
        "source": source,
        "doc_id": doc.doc_id,
        "period": period,
        "date": doc.date,
        "entity": doc.entity,
        "doc_type": st.get("doc_type"),
        "agency": doc.entity if source == "federal_register" else None,
        "subagency": st.get("agency_sub"),
        "sic": st.get("sic"),
        "state_incorporation": st.get("state_incorporation"),
        "state_business": st.get("state_business"),
        "fiscal_year_end": st.get("fiscal_year_end"),
        "filer_category": st.get("filer_category"),
        "items_json": json.dumps(items, sort_keys=True) if items is not None else None,
        "cfr_json": json.dumps(cfr) if cfr is not None else None,
        "significant": st.get("significant"),
        "topics_json": json.dumps(topics) if topics is not None else None,
        "format_json": json.dumps(st["format"], sort_keys=True) if st.get("format") else None,
        "n_chars": st.get("n_chars"),
        "facts_n_concepts": st.get("facts_n_concepts"),
    }


# --- Estado de avance --------------------------------------------------------------------------


class TextState:
    """Periodos procesados por fuente, en `ingest_state/text/<fuente>.json` (escritura atómica)."""

    def __init__(self, processed_dir: Path, source: str) -> None:
        self.path = processed_dir / "ingest_state" / "text" / f"{source}.json"
        self.periods: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            self.periods = json.loads(self.path.read_text(encoding="utf-8"))["periods"]

    def is_done(self, period: str) -> bool:
        return bool(self.periods.get(period, {}).get("closed"))

    def needs_run(
        self, period: str, refresh_hours: float, now: datetime, min_schema: int = 1
    ) -> bool:
        """Cerrado y procesado con un esquema >= `min_schema`: nunca más. Abierto: si su última
        pasada tiene más de `refresh_hours` (así un bucle de tramos termina aunque el periodo
        siga abierto)."""
        info = self.periods.get(period)
        if info is None:
            return True
        if info.get("schema", 1) < min_schema:
            return True
        if info.get("incomplete"):
            return True
        if info.get("closed"):
            return False
        last = datetime.fromisoformat(info["processed_at"])
        return (now - last).total_seconds() > refresh_hours * 3600

    def mark(self, period: str, *, closed: bool, run_id: str, stats: dict[str, Any]) -> None:
        self.periods[period] = {
            "closed": closed,
            "incomplete": stats.get("incomplete", False),
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "processed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "docs": stats["docs"],
            "rows": stats["rows"],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"periods": self.periods}, indent=1, sort_keys=True))
        os.replace(tmp, self.path)


# --- Corrida -----------------------------------------------------------------------------------


def run_source(
    src: TextSource,
    cfg: dict[str, Any],
    *,
    processed_dir: Path,
    run_id: str,
    today: date | None = None,
    max_periods: int | None = None,
    only_periods: list[str] | None = None,
    sample: int | None = None,
    workers: int | None = None,
    logger: Any = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Procesa los periodos pendientes (o `only_periods`), uno por tramo, y marca el estado.

    `deadline` (época en segundos): presupuesto de la corrida (ADR 0026). Antes de cada periodo se
    comprueba que quepa: si el tiempo que queda es menor que `text.period_margin` veces el periodo
    más lento de esta corrida (o la estimación de `text.period_seconds_estimate` si aún no hay
    ninguno), se corta con `budget_exhausted`; la próxima corrida sigue con los pendientes."""
    today = today or date.today()
    text_cfg = cfg["text"]
    state = TextState(processed_dir, src.name)
    now = datetime.now(UTC)
    refresh = text_cfg["open_refresh_hours"]
    min_schema = cfg[src.name].get("reprocess_below_schema", 1)
    candidates = only_periods or [
        p for p in src.periods(today) if state.needs_run(p, refresh, now, min_schema)
    ]
    todo = candidates[:max_periods] if max_periods is not None else candidates
    n_workers = text_cfg["workers"] if workers is None else workers
    # forkserver: el proceso principal tiene hilos (httpx) y fork() con hilos puede bloquearse.
    executor = (
        ProcessPoolExecutor(n_workers, mp_context=multiprocessing.get_context("forkserver"))
        if n_workers > 0
        else None
    )
    done: list[dict[str, Any]] = []
    budget_exhausted = False
    try:
        for period in todo:
            if deadline is not None:
                slowest = max((p["seconds"] for p in done), default=None)
                need = (
                    slowest
                    if slowest is not None
                    else text_cfg["period_seconds_estimate"][src.name]
                ) * text_cfg["period_margin"]
                if clock() + need > deadline:
                    budget_exhausted = True
                    break
            t0 = time.perf_counter()
            tmp_dir = Path(tempfile.mkdtemp(prefix=f"text-{src.name}-{period}-"))
            try:
                ctx = CollectContext(
                    tmp_dir=tmp_dir,
                    executor=executor,
                    min_chars=cfg[src.name]["min_chars"],
                    n_max=text_cfg["ngram_max"],
                    sample=sample,
                    seed=text_cfg["seed"],
                    max_in_flight=max(4, 4 * n_workers),
                )
                docs = src.collect(period, ctx)
                stats = finalize_period(
                    src.name,
                    period,
                    docs,
                    processed_dir=processed_dir,
                    run_id=run_id,
                    min_doc_count=text_cfg["min_doc_count"],
                    top_sic=text_cfg["top_sic"],
                    n_max=text_cfg["ngram_max"],
                )
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if src.name == "edgar":
                attempted = [
                    d
                    for d in docs
                    if not any((sec.detail or "").startswith("formulario=") for sec in d.sections)
                ]
                failed = sum(
                    any((sec.detail or "").startswith("error_descarga=") for sec in d.sections)
                    for d in attempted
                )
                incomplete = any(
                    sec.detail == "error_descarga=DecodingError"
                    for d in attempted
                    for sec in d.sections
                )
                stats.update(
                    download_attempted=len(attempted),
                    download_failed=failed,
                    download_succeeded=len(attempted) - failed,
                    incomplete=incomplete,
                )
            # Una muestra nunca cierra un periodo: el universo completo queda pendiente.
            closed = sample is None and src.is_closed(period, today) and not stats.get("incomplete")
            if sample is None:
                state.mark(period, closed=closed, run_id=run_id, stats=stats)
            stats["seconds"] = round(time.perf_counter() - t0, 1)
            stats["closed"] = closed
            done.append(stats)
            if logger is not None:
                logger.info("periodo", extra={"fields": {"source": src.name, **stats}})
    finally:
        if executor is not None:
            executor.shutdown()
    return {
        "source": src.name,
        "periods_done_now": done,
        "pending": 0 if only_periods else len(candidates) - len(done),
        "budget_exhausted": budget_exhausted,
        "incomplete": any(p.get("incomplete", False) for p in done),
    }


def migrate(processed_dir: Path) -> dict[str, int]:
    """Lleva las particiones de `term_freq` al esquema actual sin descargar nada (idempotente).

    Las escritas antes de la versión 2 no tienen `n_entities`: se reescriben con la columna en
    NULL ("no medido"; nunca un valor inventado). Las que ya la tienen no se tocan.
    """
    import pyarrow.parquet as pq

    root = processed_dir / TABLE_DIRS[TERM_FREQ.name]
    out = {"particiones": 0, "migradas": 0}
    for part in sorted(p for p in root.glob("*/*") if p.is_dir() and "." not in p.name):
        files = sorted(part.glob("*.parquet"))
        if not files:
            continue
        out["particiones"] += 1
        if all(set(TERM_FREQ.schema.names) <= set(pq.read_schema(f).names) for f in files):
            continue
        table = pa.concat_tables([pq.read_table(f) for f in files], promote_options="default")
        if "n_entities" not in table.column_names:
            table = table.append_column("n_entities", pa.nulls(table.num_rows, pa.int64()))
        if "n_industries" not in table.column_names:
            table = table.append_column("n_industries", pa.nulls(table.num_rows, pa.int64()))
        if "sic_top_json" not in table.column_names:
            table = table.append_column("sic_top_json", pa.nulls(table.num_rows, pa.string()))
        store.replace_partition(
            table.select(TERM_FREQ.schema.names).cast(TERM_FREQ.schema),
            TERM_FREQ,
            root,
            source=part.parent.name,
            partition=part.name,
        )
        out["migradas"] += 1
    return out


def summary(processed_dir: Path) -> dict[str, Any]:
    """Conteos de verificación de las tablas de texto (copia local)."""
    import duckdb

    con = duckdb.connect()
    out: dict[str, Any] = {}
    for name, sub in TABLE_DIRS.items():
        files = sorted((processed_dir / sub).glob("*/*/*.parquet"))
        if not files:
            out[name] = {}
            continue
        rel = f"read_parquet({[str(f) for f in files]!r})"
        if name == TERM_FREQ.name:
            sql = (
                f"SELECT source, section, count(DISTINCT period), count(*), "
                f"count(DISTINCT term) FROM {rel} GROUP BY 1, 2 ORDER BY 1, 2"
            )
            out[name] = [
                dict(zip(("source", "section", "periods", "rows", "terms"), r, strict=True))
                for r in con.execute(sql).fetchall()
            ]
        elif name == TEXT_INGEST.name:
            sql = f"SELECT source, status, count(*) FROM {rel} GROUP BY 1, 2 ORDER BY 1, 2"
            out[name] = [list(r) for r in con.execute(sql).fetchall()]
        else:
            sql = (
                f"SELECT source, section, count(*), min(date), max(date) FROM {rel} "
                "GROUP BY 1, 2 ORDER BY 1, 2"
            )
            out[name] = [list(map(str, r)) for r in con.execute(sql).fetchall()]
        out[f"{name}_files"] = len(files)
        out[f"{name}_bytes"] = sum(f.stat().st_size for f in files)
    return out


def build_source(name: str, cfg: dict[str, Any], raw_dir: Path) -> TextSource:
    if name == "edgar":
        from src.ingest.edgar import EdgarSource

        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if not user_agent:
            raise SystemExit(
                "SEC_USER_AGENT no está definida: la SEC exige 'Nombre Apellido correo' "
                "(ver .env.example)"
            )
        return EdgarSource.from_config(cfg, user_agent, raw_dir)
    from src.ingest.federal_register import FederalRegisterSource

    return FederalRegisterSource.from_config(cfg, raw_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.ingest.text")
    parser.add_argument(
        "command",
        choices=["edgar", "federal_register", "summary", "migrate", "structure-report"],
    )
    parser.add_argument("--max-periods", type=int, default=None)
    parser.add_argument("--periods", default=None, help="lista separada por comas")
    parser.add_argument("--sample", type=int, default=None, help="documentos al azar por periodo")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument(
        "--deadline-epoch",
        type=float,
        default=None,
        help="época (s) hasta la que se pueden iniciar periodos; no empieza uno que no quepa",
    )
    parser.add_argument("--processed-dir", type=Path, default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs" / "estructura-texto.md",
        help="structure-report: archivo de salida",
    )
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    cfg = load_config()
    if args.sample is not None and args.processed_dir is None:
        parser.error("--sample escribe una muestra: exige --processed-dir fuera de data/processed")
    processed = args.processed_dir or _resolve(cfg["paths"]["processed_dir"])
    if args.command == "summary":
        print(json.dumps(summary(processed), indent=2, default=str))
        return 0
    if args.command == "structure-report":
        from src.ingest.text_structure import write_report

        print(write_report(processed, args.out, cfg))
        return 0
    if args.command == "migrate":
        print(json.dumps(migrate(processed)))
        return 0
    logger = get_logger(f"ingest.{args.command}", cfg["logging"]["level"])
    src = build_source(args.command, cfg, _resolve(cfg["paths"]["raw_dir"]))
    try:
        with run([src.name], _resolve(cfg["logging"]["runs_file"])) as record:
            with record.stage(args.command):
                stats = run_source(
                    src,
                    cfg,
                    processed_dir=processed,
                    run_id=record.run_id,
                    max_periods=args.max_periods,
                    only_periods=args.periods.split(",") if args.periods else None,
                    sample=args.sample,
                    workers=args.workers,
                    logger=logger,
                    deadline=args.deadline_epoch,
                )
            record.n_series[src.name] = sum(p["docs"] for p in stats["periods_done_now"])
            record.details = {"command": args.command, "stats": stats}
        print(json.dumps(stats, default=str, indent=2))
    finally:
        src.close()
    if stats["incomplete"]:
        return EXIT_INCOMPLETE
    if stats["budget_exhausted"]:
        return EXIT_BUDGET
    return EXIT_PENDING if stats["pending"] else 0


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


if __name__ == "__main__":
    sys.exit(main())
