"""Lista «en observación» (🟡): fenómenos de un solo origen con evidencia de demanda (ADR 0016).

Excepción al principio 4 aprobada por David el 2026-11-10: esta lista separada NO es el ranking
final, no entra al score y sus fenómenos no se presentan como triangulados. El ranking principal no
cambia: sigue exigiendo ≥ 2 fuentes independientes por origen.

Un fenómeno entra si (todo sobre su evidencia pública: nada con copyright de terceros):
1. tiene un solo origen (`status == "una_fuente"` y exactamente 1 grupo de fuentes),
2. tiene señal: q ≤ `fdr.q` (las señales ya pasaron BH; se vuelve a exigir),
3. tiene al menos una serie `cantidad` o `valor` (clase de medida, ADR 0014) en un cluster al que
   no se le marcó efecto de tasas ni efecto inflación (ADR 0013/0015): una serie nominal que solo
   refleja la inflación o las tasas no es evidencia de demanda, y
4. el efecto de esa serie (estandarizado por la sd robusta de su crecimiento) es ≥ `min_effect`.

Los clusters con efecto no evaluado (NULL) no se excluyen: el fallo es no saber, no un efecto.
Cada fenómeno que no entra lleva la primera razón que lo deja fuera, para auditar el embudo.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.rank.triangulate import Candidate, Evidence

# Clases de medida que cuentan como evidencia de demanda (ADR 0014).
DEMAND_KINDS = frozenset({"cantidad", "valor"})


@dataclass(frozen=True)
class WatchParams:
    q: float  # fdr.q
    min_effect: float  # efecto estandarizado mínimo


@dataclass
class Watch:
    eligible: bool
    reason: str | None = None  # por qué no entra (None si entra)
    effect: float | None = None  # mayor |efecto| de sus series de demanda
    q_value: float | None = None  # menor q de esas series
    n_demand_series: int = 0


def assess_watch(
    candidate: Candidate,
    cluster_evidence: Mapping[str, list[Evidence]],
    marked: Mapping[str, bool | None],
    params: WatchParams,
) -> Watch:
    """Decide si un fenómeno (`candidate`, de un solo origen) entra en la lista de observación.

    `cluster_evidence`: id de cluster -> su evidencia; `marked`: id de cluster -> True si tiene
    efecto de tasas o de inflación (None/False: sin marca)."""
    if candidate.status != "una_fuente":
        return Watch(False, "triangulado")
    if candidate.n_sources != 1:
        return Watch(False, "sin_fuente")
    series = [e for ev in cluster_evidence.values() for e in ev if e.kind == "series" and e.public]
    if not series:
        return Watch(False, "sin_evidencia_publica")
    sig = [e for e in series if e.q_value <= params.q]
    if not sig:
        return Watch(False, "sin_senal")
    demand = [e for e in sig if e.measure_kind in DEMAND_KINDS]
    if not demand:
        return Watch(False, "sin_evidencia_de_demanda")
    clean = [
        e
        for cid, ev in cluster_evidence.items()
        if not marked.get(cid)
        for e in ev
        if any(e is d for d in demand)
    ]
    if not clean:
        return Watch(False, "efecto_tasas_o_inflacion")
    big = [e for e in clean if abs(e.effect_size) >= params.min_effect]
    if not big:
        return Watch(False, "efecto_bajo")
    return Watch(
        True,
        None,
        effect=max(abs(e.effect_size) for e in big),
        q_value=min(e.q_value for e in big),
        n_demand_series=len(big),
    )
