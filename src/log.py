"""Logs estructurados (JSON por línea) y registro de corridas."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    """Formatea cada registro como un objeto JSON en una línea."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """Logger con salida JSON a stderr; idempotente."""
    logger = logging.getLogger(name)
    if not any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(level)
    return logger


class RunRecord:
    """Registro de una ejecución: fecha, fuentes, número de series y tiempos por etapa."""

    def __init__(self, sources: list[str]) -> None:
        self.run_id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC)
        self.sources = sources
        self.n_series: dict[str, int] = {}
        self.stage_seconds: dict[str, float] = {}
        self.details: dict[str, Any] = {}
        self.status = "running"

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Mide la duración de una etapa y la acumula en el registro."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stage_seconds[name] = self.stage_seconds.get(name, 0.0) + (
                time.perf_counter() - t0
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "sources": self.sources,
            "n_series": self.n_series,
            "stage_seconds": {k: round(v, 3) for k, v in self.stage_seconds.items()},
            "status": self.status,
            "details": self.details,
        }


@contextmanager
def run(sources: list[str], runs_file: Path | str) -> Iterator[RunRecord]:
    """Contexto de corrida: al salir (con o sin error) añade el registro a `runs_file`."""
    record = RunRecord(sources)
    t0 = time.perf_counter()
    try:
        yield record
        record.status = "ok"
    except BaseException:
        record.status = "failed"
        raise
    finally:
        out = record.to_dict()
        out["finished_at"] = datetime.now(UTC).isoformat()
        out["total_seconds"] = round(time.perf_counter() - t0, 3)
        path = Path(runs_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
