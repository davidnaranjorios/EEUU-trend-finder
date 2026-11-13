"""Marcas descriptivas que NO filtran (ADR 0025): se validan a mano después.

- `sin_evidencia_demanda`: ninguna serie de cantidad (no poblacional) ni de valor cuyo efecto real
  (deflactado) tenga el mismo signo que el de la señal. A diferencia de la lista «en observación»
  no exige un umbral de efecto: solo avisa de que el respaldo numérico son precios, tasas o valores
  nominales sin efecto real. Con la inflación sin medir (efecto real None) cuenta como sin demanda.
- `origen_posiblemente_compartido`: dos series de fuentes distintas del candidato comparten un
  eslabón de la cadena de origen (`config/data_origin.yaml`), o alguna tiene posibles copias
  detectadas por los datos (`origin.find_duplicates`). La triangulación ya unifica las copias
  exactas; la marca avisa de las que quedan.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.rank.triangulate import Evidence


def has_demand(e: Evidence) -> bool:
    if e.kind != "series":
        return False
    if e.measure_kind == "cantidad":
        return e.measure_subtype != "poblacion"
    return (
        e.measure_kind == "valor"
        and e.real_effect is not None
        and e.real_effect * e.effect_size > 0
    )


def sin_evidencia_demanda(evidence: Iterable[Evidence]) -> bool | None:
    """True si no hay evidencia de demanda; None si el candidato no tiene series (solo texto)."""
    series = [e for e in evidence if e.kind == "series"]
    if not series:
        return None
    return not any(has_demand(e) for e in series)


def origen_posiblemente_compartido(evidence: Iterable[Evidence]) -> bool | None:
    series = [e for e in evidence if e.kind == "series"]
    if not series:
        return None
    if any(e.possible_duplicates for e in series):
        return True
    for i, a in enumerate(series):
        for b in series[i + 1 :]:
            if a.agencies != b.agencies and set(a.chain) & set(b.chain):
                return True
    return False
