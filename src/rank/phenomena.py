"""Agrupación en dos niveles: clusters de series → fenómenos (auditoría f9189307).

Un mismo fenómeno puede partirse en varios clusters (BLS y Census miden el mismo sector con
series que no se parecen en título ni en crecimiento). Un fenómeno une los clusters que:

1. comparten industria NAICS a `rank.phenomena.naics_digits` dígitos (un código es compatible con
   otro si uno es prefijo del otro tras truncar; los rangos "31-33" se expanden a sus sectores),
2. tienen la misma dirección del efecto (signo mediano de sus series; un cluster sin una dirección
   clara —menos de `min_direction_share` de acuerdo— no se une a nadie), y
3. tienen ventanas de señal que se solapan.

La unión es transitiva (componentes conexas) pero solo sobre estas tres condiciones, sin mapeos
escritos a mano: el NAICS sale de las tablas oficiales y de los datos (naics.py).

La triangulación del fenómeno (`triangulate.assess`) cuenta las fuentes independientes por ORIGEN
sobre la evidencia de todos sus clusters; no se relaja la regla de origen (ADR 0010): dos clusters
con el mismo origen siguen siendo una sola fuente.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from src.rank import measure as measure_mod
from src.rank.cluster import cluster_id
from src.rank.triangulate import Candidate, Evidence, assess


@dataclass(frozen=True)
class PhenomenaParams:
    naics_digits: int = 3
    min_naics_share: float = 0.25
    min_direction_share: float = 0.6


@dataclass
class Phenomenon:
    phenomenon_id: str
    cluster_ids: list[str]
    naics: list[tuple[str, float]]  # (código, peso) del fenómeno
    direction: str  # "sube" | "baja" | "mixto"
    window_start: Any
    window_end: Any
    candidate: Candidate = field(repr=False, default=None)  # type: ignore[assignment]
    measure_mix: dict[str, int] = field(default_factory=dict)  # clases de medida de sus series
    no_demand_evidence: bool | None = None  # todo su respaldo son precios (ADR 0014)


def code_prefixes(code: str, digits: int) -> list[str]:
    """Prefijos comparables de un código NAICS: rangos expandidos, el resto truncado."""
    if "-" in code:
        lo, hi = code.split("-", 1)
        return [str(i) for i in range(int(lo), int(hi) + 1)]
    return [code[:digits]]


def compatible(a: list[str], b: list[str]) -> bool:
    return any(x.startswith(y) or y.startswith(x) for x in a for y in b)


def cluster_naics(
    members: list[tuple[str, str]],
    links: Mapping[tuple[str, str], list[tuple[str, float, str]]],
    min_share: float,
) -> list[tuple[str, float]]:
    """NAICS del cluster: promedio de pesos de sus miembros con anclaje, sin códigos marginales."""
    have = [k for k in members if k in links]
    acc: dict[str, float] = defaultdict(float)
    for k in have:
        for code, w, _ in links[k]:
            acc[code] += w / len(have)
    return sorted(
        ((c, w) for c, w in acc.items() if w >= min_share), key=lambda cw: (-cw[1], cw[0])
    )


def direction_of(evidence: list[Evidence], min_share: float) -> str:
    """Signo mediano del efecto de las series; "mixto" si no hay acuerdo ≥ `min_share`."""
    eff = [e.effect_size for e in evidence if e.kind == "series" and e.effect_size != 0]
    if not eff:
        return "mixto"
    pos = sum(x > 0 for x in eff) / len(eff)
    if pos >= min_share and np.median(eff) > 0:
        return "sube"
    if (1 - pos) >= min_share and np.median(eff) < 0:
        return "baja"
    return "mixto"


def window_of(evidence: list[Evidence]) -> tuple[Any, Any]:
    starts = [e.window_start for e in evidence if e.kind == "series" and e.window_start]
    ends = [e.window_end for e in evidence if e.kind == "series" and e.window_end]
    return (min(starts), max(ends)) if starts and ends else (None, None)


def group_phenomena(
    clusters: list[Candidate],
    members_of: Mapping[str, list[tuple[str, str]]],
    links: Mapping[tuple[str, str], list[tuple[str, float, str]]],
    params: PhenomenaParams,
    min_sources: int,
) -> list[Phenomenon]:
    """Particiona los clusters de series en fenómenos y triangula cada uno."""
    series = [c for c in clusters if c.kind == "series"]
    info: dict[str, dict[str, Any]] = {}
    for c in series:
        ws, we = window_of(c.evidence)
        naics = cluster_naics(members_of[c.candidate_id], links, params.min_naics_share)
        pref: list[str] = []
        for code, _ in naics:
            pref += code_prefixes(code, params.naics_digits)
        info[c.candidate_id] = {
            "naics": naics,
            "prefixes": pref,
            "direction": direction_of(c.evidence, params.min_direction_share),
            "ws": ws,
            "we": we,
        }
    parent = {c.candidate_id: c.candidate_id for c in series}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # Cubos por sector de 2 dígitos: solo se comparan clusters que comparten sector.
    buckets: dict[str, list[str]] = defaultdict(list)
    for cid, i in info.items():
        if i["direction"] == "mixto" or i["ws"] is None:
            continue
        for sector in {p[:2] for p in i["prefixes"]}:
            buckets[sector].append(cid)
    for ids in buckets.values():
        for ai, a in enumerate(ids):
            for b in ids[ai + 1 :]:
                ia, ib = info[a], info[b]
                if (
                    ia["direction"] == ib["direction"]
                    and ia["ws"] <= ib["we"]
                    and ib["ws"] <= ia["we"]
                    and compatible(ia["prefixes"], ib["prefixes"])
                ):
                    parent[find(a)] = find(b)

    groups: dict[str, list[Candidate]] = defaultdict(list)
    for c in series:
        groups[find(c.candidate_id)].append(c)
    out: list[Phenomenon] = []
    for grp in groups.values():
        grp.sort(key=lambda c: c.candidate_id)
        seen: set[tuple[str, str, str]] = set()
        ev: list[Evidence] = []
        for c in grp:
            for e in c.evidence:
                if (e.kind, e.source, e.object_id) not in seen:
                    seen.add((e.kind, e.source, e.object_id))
                    ev.append(e)
        cids = [c.candidate_id for c in grp]
        pid = cluster_id("p", cids)
        cand = assess(Candidate(pid, "phenomenon", ev, sum(c.n_members for c in grp)), min_sources)
        kinds = [e.measure_kind for e in ev if e.kind == "series"]
        ws, we = window_of(ev)
        acc: dict[str, float] = defaultdict(float)
        for c in grp:
            for code, w in info[c.candidate_id]["naics"]:
                acc[code] += w / len(grp)
        dirs = {info[c.candidate_id]["direction"] for c in grp}
        out.append(
            Phenomenon(
                pid,
                cids,
                sorted(acc.items(), key=lambda cw: (-cw[1], cw[0]))[:5],
                dirs.pop() if len(dirs) == 1 else "mixto",
                ws,
                we,
                cand,
                measure_mod.mix(kinds),
                measure_mod.no_demand_evidence(kinds),
            )
        )
    return sorted(out, key=lambda p: p.phenomenon_id)
