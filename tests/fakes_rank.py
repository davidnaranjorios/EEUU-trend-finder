"""Fixture sintético de la Fase 5: señales numéricas y de texto ya calculadas, sin red.

Todo título y término es una pseudopalabra aleatoria (ningún tema real). Casos:
- `dup`: la misma serie (mismos valores y título) en FRED (agencia BLS) y en BLS, con señal.
  Mismo dato, misma agencia -> un cluster, una sola fuente -> no es candidato.
- `linked`: serie de BEA con señal y un término de EDGAR con señal cuyo texto es su título
  -> dos fuentes independientes -> candidato publicado.
- `cr`: serie de FRED **con copyright** con señal y un término del Federal Register enlazado
  -> candidato (2 fuentes), pero su evidencia pública es una sola -> no se publica.
- `noagency`: serie de FRED sin agencias conocidas con señal y sin texto -> una fuente.
- `twin`: el mismo término emergente en EDGAR y en el Federal Register -> candidato solo de texto.
- `filler`: series probadas sin señal (universo de los cruces y del explorador).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa

from src.config import load_config
from src.ingest.fred import SERIES_ATTRS
from src.ingest.store import replace_partition, upsert
from src.schemas import (
    DETECTOR_RUNS,
    DETECTOR_TESTS,
    ELIGIBILITY,
    SERIES,
    SERIES_META,
    SIGNALS,
    TERM_FREQ,
)

N_MONTHS = 240
NUM_RUN = "num-run"
TEXT_RUN = {"edgar": "edgar-run", "federal_register": "fr-run"}
ALPHABET = np.array(list("bcdfghjklmnpqrstvwxz"))
VOWELS = np.array(list("aeiou"))


def pseudo_word(rng: np.random.Generator, n: int = 4) -> str:
    return "".join(
        rng.choice(ALPHABET) + rng.choice(VOWELS) for _ in range(int(rng.integers(2, n + 1)))
    )


def phrase(rng: np.random.Generator, k: int = 3) -> str:
    return " ".join(pseudo_word(rng) for _ in range(k))


def month(i: int) -> date:
    return date(2006 + i // 12, i % 12 + 1, 1)


@dataclass
class Fixture:
    cfg: dict
    ids: dict[str, tuple[str, str]] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    terms: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def make_cfg(tmp_path: Path) -> dict:
    cfg = copy.deepcopy(load_config())
    data = tmp_path / "data"
    cfg["paths"]["processed_dir"] = str(data / "processed")
    cfg["paths"]["signals_dir"] = str(data / "signals")
    cfg["logging"]["runs_file"] = str(data / "logs" / "runs.jsonl")
    cfg["report"]["site_dir"] = str(data / "site")
    cfg["report"]["reports_dir"] = str(data / "reports")
    cfg["report"]["runs_file"] = str(data / "logs" / "reporte" / "runs.jsonl")
    cfg["rank"]["embeddings"]["backend"] = "hashing"
    cfg["rank"]["links"]["null_sample"] = 300
    cfg["detect"]["workers"] = 1
    return cfg


def build(tmp_path: Path, seed: int = 7) -> Fixture:
    cfg = make_cfg(tmp_path)
    fx = Fixture(cfg)
    rng = np.random.default_rng(seed)
    processed = Path(cfg["paths"]["processed_dir"])
    signals_dir = Path(cfg["paths"]["signals_dir"])
    series_rows, meta, elig, attrs, tests, sigs = [], [], [], [], [], []
    decided = datetime(2026, 11, 1, tzinfo=UTC)

    def add_series(
        name,
        source,
        sid,
        title,
        growth,
        *,
        signal,
        copyrighted=False,
        agencies=None,
        units="Index",
        category=None,
        sa="SA",
        detect=True,
        release_ids=None,
    ):
        level = 100 * np.exp(np.cumsum(growth))
        series_rows.extend(
            {
                "source": source,
                "series_id": sid,
                "date": month(i),
                "value": float(v),
                "vintage_date": None,
                "frequency": "M",
                "units": units,
                "seasonal_adjustment": sa,
            }
            for i, v in enumerate(level)
        )
        meta.append(
            {
                "source": source,
                "series_id": sid,
                "title": title,
                "category_path": category,
                "start_date": month(0),
                "end_date": month(N_MONTHS - 1),
                "last_updated": decided,
                "discontinued": False,
                "copyrighted": copyrighted,
            }
        )
        elig.append(
            {
                "run_id": "elig1",
                "source": source,
                "series_id": sid,
                "group_name": "nacional",
                "rule": "clasificada",
                "detail": None,
                "geo_level": "nacional",
                "frequency": "M",
                "copyrighted": copyrighted,
                "decided_at": decided,
            }
        )
        if source == "fred":
            attrs.append(
                {
                    "source": "fred",
                    "series_id": sid,
                    "geo_types": [],
                    "is_usa": True,
                    "release_ids": release_ids or [1],
                    "agencies": agencies or [],
                    "popularity": 1,
                }
            )
        for det in ("slope", "regime") if detect else ():
            q = 1e-6 if signal else 0.9
            row = {
                "run_id": NUM_RUN,
                "detector": det,
                "source": source,
                "object_id": sid,
                "frequency": "M",
                "grid": "M",
                "n_growth": 179,
                "block_length": 3.0,
                "statistic": 5.0 if signal else 0.1,
                "effect_size": 2.0 if signal else 0.1,
                "persistent": signal,
                "p_value": q / 10,
                "p_method": "gpd",
                "q_value": q,
                "q_by": min(1.0, q * 5),
                "n_tests": 1000,
                "window_start": month(N_MONTHS - 60),
                "window_end": month(N_MONTHS - 1),
                "copyrighted": copyrighted,
            }
            tests.append(row)
            if signal:
                sigs.append(
                    {
                        "signal_id": f"{det}-{source}-{sid}",
                        "source": source,
                        "object_id": sid,
                        "detector": det,
                        "window_start": row["window_start"],
                        "window_end": row["window_end"],
                        "effect_size": 2.0,
                        "p_value": q / 10,
                        "q_value": q,
                        "run_date": date(2026, 11, 2),
                        "run_id": NUM_RUN,
                        "n_tests": 1000,
                    }
                )
        fx.ids[name] = (source, sid)
        fx.titles[name] = title

    def trend() -> np.ndarray:
        g = 0.002 + 0.01 * rng.standard_normal(N_MONTHS)
        g[-60:] += 0.03
        return g

    dup_title, dup_growth = phrase(rng), trend()
    add_series(
        "dup_fred",
        "fred",
        "DUPF",
        dup_title,
        dup_growth,
        signal=True,
        agencies=["U.S. Bureau of Labor Statistics"],
    )
    add_series("dup_bls", "bls", "DUPB", dup_title, dup_growth, signal=True)
    cat = "Rama sintetica > " + pseudo_word(rng)
    add_series(
        "linked",
        "bea",
        "LNK",
        phrase(rng) + " total",
        trend(),
        signal=True,
        units="Millions of Dollars",
        category=cat,
    )
    add_series(
        "cr",
        "fred",
        "CRS",
        phrase(rng) + " composite",
        trend(),
        signal=True,
        copyrighted=True,
        agencies=["Third Party Publisher"],
    )
    # Distintas agencias y el mismo dato (ρ ≈ 1, nivel igual): "posible mismo origen" (ADR 0010).
    mir_title, mir_growth = phrase(rng), trend()
    add_series(
        "mirror_a", "fred", "MIRA", mir_title, mir_growth, signal=True, agencies=["Agency One"]
    )
    add_series(
        "mirror_b", "fred", "MIRB", mir_title, mir_growth, signal=True, agencies=["Agency Two"]
    )
    # Republicaciones de la tabla data_origin: Z.1 (bancos) y FDIC QBP -> Call Reports.
    add_series(
        "z1_bank",
        "fred",
        "BOGZ1FL763065105Q",
        phrase(rng),
        trend(),
        signal=True,
        agencies=["Board of Governors of the Federal Reserve System"],
        release_ids=[52],
    )
    add_series(
        "fdic_bank",
        "fred",
        "FDICQ1",
        phrase(rng),
        trend(),
        signal=True,
        agencies=["Federal Deposit Insurance Corporation"],
        release_ids=[482],
    )
    add_series("noagency", "fred", "NOAG", phrase(rng), trend(), signal=True, agencies=[])
    for k in range(400):
        src = ("fred", "bls", "bea", "census")[k % 4]
        add_series(
            f"filler{k}",
            src,
            f"FIL{k}",
            phrase(rng),
            0.002 + 0.01 * rng.standard_normal(N_MONTHS),
            signal=False,
            agencies=["Filler Agency"],
            category=cat if k % 50 == 0 else None,
            units="Percent" if k % 100 == 0 else "Index",
        )
    for k in range(10):  # copyrighted sin señal: no deben aparecer en el explorador
        add_series(
            f"crfill{k}",
            "fred",
            f"CRF{k}",
            phrase(rng) + " licensed",
            0.002 + 0.01 * rng.standard_normal(N_MONTHS),
            signal=False,
            copyrighted=True,
            agencies=["Third Party Publisher"],
            category=cat,  # misma rama que "linked": nunca deben salir como relacionadas
        )
    # Series de referencia metodológica (context.reference_series). El deflactor lleva una
    # "señal" fingida para comprobar que no entra al ranking.
    refs = fx.cfg["context"]["reference_series"]
    add_series(
        "ref_deflator",
        "fred",
        refs["deflator"],
        "Price level reference",
        np.full(N_MONTHS, 0.002),
        signal=True,
        agencies=["Reference Agency"],
    )
    add_series(
        "ref_population",
        "fred",
        refs["population"],
        "Population reference",
        np.full(N_MONTHS, 0.0008),
        signal=False,
        units="Thousands",
        agencies=["Reference Agency"],
    )
    add_series(
        "ref_recessions",
        "fred",
        refs["recessions"],
        "Recession indicator",
        np.zeros(N_MONTHS),
        signal=False,
        agencies=["Reference Agency"],
        detect=False,
    )
    add_series(  # tasa de corto plazo de referencia: con "señal" fingida, tampoco entra al ranking
        "ref_short_rate",
        "fred",
        refs["short_rate"],
        "Short-term rate reference",
        0.01 * rng.standard_normal(N_MONTHS),
        signal=True,
        units="Percent",
        agencies=["Reference Agency"],
    )
    rec = np.zeros(N_MONTHS)
    rec[24:36] = 1  # una recesión de 12 meses
    for r in series_rows:  # el indicador de recesión es 0/1, no un nivel exponencial
        if r["series_id"] == refs["recessions"]:
            r["value"] = float(rec[(r["date"].year - 2006) * 12 + r["date"].month - 1])

    for src in ("fred", "bls", "bea", "census"):

        def part(rows, src=src):
            return [r for r in rows if r["source"] == src]

        upsert(
            pa.Table.from_pylist(part(series_rows), schema=SERIES.schema),
            SERIES,
            processed / "series",
            source=src,
            n_buckets=4,
        )
        upsert(
            pa.Table.from_pylist(part(meta), schema=SERIES_META.schema),
            SERIES_META,
            processed / "series_meta",
            source=src,
            n_buckets=4,
        )
        upsert(
            pa.Table.from_pylist(part(elig), schema=ELIGIBILITY.schema),
            ELIGIBILITY,
            processed / "lineage" / "eligibility",
            source=src,
            n_buckets=4,
        )
        upsert(
            pa.Table.from_pylist(part(tests), schema=DETECTOR_TESTS.schema),
            DETECTOR_TESTS,
            processed / "lineage" / "detector_tests",
            source=src,
            n_buckets=4,
            bucket_col="object_id",
        )
        if part(sigs):
            upsert(
                pa.Table.from_pylist(part(sigs), schema=SIGNALS.schema),
                SIGNALS,
                signals_dir,
                source=src,
                n_buckets=4,
                bucket_col="object_id",
            )
    upsert(
        pa.Table.from_pylist(attrs, schema=SERIES_ATTRS.schema),
        SERIES_ATTRS,
        processed / "_internal" / "fred_series_attrs",
        source="fred",
        n_buckets=4,
    )
    runs = [
        _run_row(NUM_RUN, det, len([t for t in tests if t["detector"] == det]))
        for det in ("slope", "regime")
    ]
    upsert(
        pa.Table.from_pylist(runs, schema=DETECTOR_RUNS.schema),
        DETECTOR_RUNS,
        processed / "lineage" / "detector_runs",
        source="numeric",
        n_buckets=4,
        bucket_col="run_id",
    )

    # Texto: términos con señal.
    twin = phrase(rng)
    text_terms = {
        "edgar": [("1A", fx.titles["linked"]), ("1A", twin), ("7", phrase(rng))],
        "federal_register": [("rule", fx.titles["cr"]), ("rule", twin)],
    }
    fx.terms = text_terms
    vocab = {src: [phrase(rng, 2) for _ in range(200)] for src in text_terms}  # sin señal
    for src, terms in text_terms.items():
        tsigs = [
            {
                "signal_id": f"t-{src}-{i}",
                "source": src,
                "object_id": f"{section}|{term}",
                "detector": "text_emerging",
                "window_start": date(2023, 1, 1),
                "window_end": date(2025, 12, 31),
                "effect_size": 1.2,
                "p_value": 1e-5,
                "q_value": 1e-4 * (i + 1),
                "run_date": date(2026, 11, 2),
                "run_id": TEXT_RUN[src],
                "n_tests": 100,
            }
            for i, (section, term) in enumerate(terms)
        ]
        upsert(
            pa.Table.from_pylist(tsigs, schema=SIGNALS.schema),
            SIGNALS,
            signals_dir,
            source=src,
            n_buckets=4,
            bucket_col="object_id",
        )
        upsert(
            pa.Table.from_pylist(
                [_run_row(TEXT_RUN[src], "text_emerging", 100)], schema=DETECTOR_RUNS.schema
            ),
            DETECTOR_RUNS,
            processed / "lineage" / "detector_runs",
            source=src,
            n_buckets=4,
            bucket_col="run_id",
        )
        # EDGAR guarda trimestres ('2024Q1'); el Federal Register, meses ('2024-05'): como la
        # ingesta real (ADR 0005). Las proporciones crecen suave desde 2022.
        for period, frac in _periods(src):
            rise = max(0.0, frac - 2022)
            rows = [
                {
                    "source": src,
                    "section": section,
                    "period": period,
                    "term": term,
                    "doc_count": int(round(5 + 3 * rise)),
                    "total_docs": 1000,
                    "n_entities": 4 + int(rise),
                }
                for section, term in terms
            ] + [
                {
                    "source": src,
                    "section": terms[0][0],
                    "period": period,
                    "term": term,
                    "doc_count": 6,
                    "total_docs": 1000,
                    "n_entities": 5,
                }
                for term in vocab[src]
            ]
            replace_partition(
                pa.Table.from_pylist(rows, schema=TERM_FREQ.schema),
                TERM_FREQ,
                processed / "term_freq",
                source=src,
                partition=period,
            )
    return fx


def _periods(src: str) -> list[tuple[str, float]]:
    """Periodos de `term_freq` como los de la ingesta real: trimestres (EDGAR) o meses (FR)."""
    if src == "edgar":
        return [(f"{y}Q{q}", y + (q - 1) / 4) for y in range(2014, 2026) for q in range(1, 5)]
    return [(f"{y}-{m:02d}", y + (m - 1) / 12) for y in range(2014, 2026) for m in range(1, 13)]


def _run_row(run_id: str, detector: str, n: int) -> dict:
    t = datetime(2026, 11, 2, tzinfo=UTC)
    return {
        "run_id": run_id,
        "detector": detector,
        "started_at": t,
        "finished_at": t,
        "n_tests": n,
        "n_bh": 0,
        "n_by": 0,
        "q": 0.05,
        "params_json": "{}",
    }
