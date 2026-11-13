"""Triangulación y score de candidatos (principio 4, ADR 0009).

Un cluster es candidato solo si su evidencia viene de al menos `rank.min_sources` (≥ 2) fuentes
independientes (src.rank.agencies). Para publicarlo, además, la evidencia **pública** (sin series
con copyright) tiene que llegar a ese mínimo por sí sola.

Score = Σ wᵢ · percentilᵢ / Σ wᵢ, con los componentes normalizados por rango (percentil con
empates promediados) dentro de los candidatos del mismo tipo (series o solo texto) en la corrida.
Pesos en `rank.weights`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from typing import Any

import numpy as np
from scipy.stats import rankdata

from src.rank.agencies import independent_groups


@dataclass
class Components:
    """Componentes del score (los nombres son las claves de `rank.weights`)."""

    strength: float = 0.0  # −log10 q, mediana por fuente independiente
    n_sources: float = 0.0
    effect: float = 0.0  # |efecto| mediano de las series (o de los términos, si no hay series)
    agreement: float = 0.0  # fracción de detectores numéricos presentes
    breadth: float = 0.0  # dispersión entre industrias (SIC) menos prevalencia base; ver abajo
    size: float = 0.0  # log(1 + miembros)


COMPONENTS = tuple(f.name for f in fields(Components))


@dataclass
class Evidence:
    kind: str  # "series" | "term"
    source: str
    object_id: str
    agencies: frozenset[str]
    q_value: float
    effect_size: float
    detectors: tuple[str, ...]
    public: bool
    window_start: Any = None
    window_end: Any = None
    link_score: float | None = None
    linked_series: str | None = None  # "fuente|serie" del miembro al que se enlazó el término
    n_entities: int | None = None
    n_industries: float | None = None  # dispersión entre industrias (SIC); None = no medida
    base_share: float | None = None  # prevalencia base del término
    link_validated: bool | None = None  # co-movimiento validado (None: no aplica)
    link_q: float | None = None
    link_lag: int | None = None
    measure_kind: str | None = None  # clase de medida de la serie (ADR 0014)
    measure_subtype: str | None = None
    real_effect: float | None = None  # efecto deflactado de una serie `valor` (ADR 0018)
    trajectory: str | None = None  # etiqueta de trayectoria de la serie (ADR 0025)
    inflation_estimated: bool | None = None  # el efecto real usó un mes de inflación interpolado
    chain: tuple[
        str, ...
    ] = ()  # cadena de origen del dato (marca `origen_posiblemente_compartido`)
    possible_duplicates: bool = False  # posibles copias de otra agencia (ADR 0010)


@dataclass
class Candidate:
    candidate_id: str
    kind: str  # "series" | "text"
    evidence: list[Evidence]
    n_members: int
    groups: list[frozenset[str]] = field(default_factory=list)
    public_groups: list[frozenset[str]] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    score: float | None = None
    rank: int | None = None
    status: str = "una_fuente"
    published: bool = False

    @property
    def n_sources(self) -> int:
        return len(self.groups)

    @property
    def n_sources_public(self) -> int:
        return len(self.public_groups)


def assess(c: Candidate, min_sources: int) -> Candidate:
    """Cuenta las fuentes independientes (todas y solo públicas) y fija el estado."""
    c.groups = independent_groups(e.agencies for e in c.evidence if e.agencies)
    c.public_groups = independent_groups(e.agencies for e in c.evidence if e.agencies and e.public)
    c.status = "candidato" if c.n_sources >= min_sources else "una_fuente"
    c.published = c.status == "candidato" and c.n_sources_public >= min_sources
    return c


def _breadth(e: Evidence) -> float | None:
    """Amplitud del término: log(1 + industrias distintas), o de entidades si no hay SIC, con
    una penalización por prevalencia base (una palabra común no es amplia por ser común): se
    divide por 1 + prevalencia / 0,05 (misma referencia que `text_detect.priority`)."""
    n = e.n_industries if e.n_industries is not None else e.n_entities
    if not n:
        return None
    return math.log1p(n) / (1.0 + (e.base_share or 0.0) / 0.05)


def raw_components(c: Candidate, detectors: Sequence[str]) -> dict[str, float]:
    """Componentes del score antes de normalizar."""
    strength = []
    for g in c.groups:
        qs = [e.q_value for e in c.evidence if e.agencies and e.agencies <= g]
        strength.append(-math.log10(max(min(qs), 1e-300)))
    series = [e for e in c.evidence if e.kind == "series"]
    effects = [abs(e.effect_size) for e in (series or c.evidence)]
    dets = {d for e in series for d in e.detectors}
    terms = [e for e in c.evidence if e.kind == "term"]
    ents = [_breadth(e) for e in terms]
    ents = [v for v in ents if v is not None]
    return asdict(
        Components(
            strength=float(np.median(strength)) if strength else 0.0,
            n_sources=float(c.n_sources),
            effect=float(np.median(effects)) if effects else 0.0,
            agreement=len(dets & set(detectors)) / max(len(detectors), 1),
            breadth=max(ents) if ents else 0.0,
            size=math.log1p(c.n_members),
        )
    )


def score(
    candidates: list[Candidate],
    weights: dict[str, float],
    detectors: Sequence[str],
    max_candidates: int,
) -> list[Candidate]:
    """Score y rango de los candidatos (solo los que tienen estado `candidato`), por tipo."""
    missing = set(COMPONENTS) - set(weights)
    if missing:
        raise ValueError(f"rank.weights sin peso para {sorted(missing)}")
    total = sum(weights[k] for k in COMPONENTS)
    for kind in sorted({c.kind for c in candidates}):
        group = [c for c in candidates if c.kind == kind and c.status == "candidato"]
        if not group:
            continue
        raw = {k: [] for k in COMPONENTS}
        for c in group:
            c.components = raw_components(c, detectors)
            for k in COMPONENTS:
                raw[k].append(c.components[k])
        if len(group) < MIN_FOR_PERCENTILES:
            pct = {k: _fixed_scale(k, v) for k, v in raw.items()}
        else:
            pct = {k: _percentile(v) for k, v in raw.items()}
        for i, c in enumerate(group):
            c.score = sum(weights[k] * pct[k][i] for k in COMPONENTS) / total if total else 0.0
        group.sort(key=lambda c: (-(c.score or 0.0), c.candidate_id))
        for r, c in enumerate(group, start=1):
            c.rank = r
            if r > max_candidates:
                c.status = "fuera_del_tope"
                c.published = False
    return candidates


# Con pocos candidatos el percentil no informa (con 1 todo vale 1; con 2, 0 o 1): se usa una
# escala fija, saturada en el valor que da el componente completo. Tope = valor de referencia
# metodológico, no un tema: −log10 q = 10, 3 fuentes, efecto de 3 desvíos robustos, amplitud
# log(1+20), tamaño log(1+50).
MIN_FOR_PERCENTILES = 5
FIXED_SCALE_MAX = {
    "strength": 10.0,
    "n_sources": 3.0,
    "effect": 3.0,
    "agreement": 1.0,
    "breadth": math.log1p(20),
    "size": math.log1p(50),
}


def _fixed_scale(component: str, values: list[float]) -> np.ndarray:
    top = FIXED_SCALE_MAX[component]
    return np.clip(np.asarray(values, dtype=float) / top, 0.0, 1.0)


def _percentile(values: list[float]) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    if v.size == 1:
        return np.ones(1)
    return (rankdata(v, method="average") - 1) / (v.size - 1)
