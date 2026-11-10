# ADR 0016 — Lista «en observación» (🟡) de fenómenos de un solo origen

Fecha: 2026-11-10. **Excepción al principio no negociable 4, aprobada por David el 2026-11-10**,
con este texto (ya añadido a `CLAUDE.md`): una lista separada «en observación» (🟡) muestra
fenómenos de un solo origen que pasan FDR y tienen evidencia de demanda (cantidad o valor); no es el
ranking final, no entra al score y sus fenómenos no se presentan como triangulados.

## Problema
La regla de origen (ADR 0010) deja fuera fenómenos con una señal estadística sólida y evidencia de
demanda pero respaldados por un único origen del dato. Hoy desaparecen del reporte.

## Decisión
- **El ranking principal no cambia:** sigue exigiendo ≥ 2 fuentes independientes por origen
  (`rank.min_sources`, nunca < 2). La lista es otra cosa: no tiene `rank` ni `score`, no es un
  candidato y no se llama «triangulada».
- **Quién entra** (`src/rank/watchlist.py`, a nivel de fenómeno, solo sobre evidencia **pública**,
  es decir sin series con copyright de terceros):
  1. un solo origen (`status = una_fuente` y exactamente un grupo de fuentes),
  2. señal: q ≤ `fdr.q` (las señales ya pasaron BH),
  3. al menos una serie `cantidad` o `valor` (ADR 0014) en un cluster **sin** efecto de tasas
     (ADR 0013) ni efecto inflación (ADR 0015),
  4. efecto estandarizado (por la sd robusta del crecimiento) ≥ `rank.watchlist.min_effect`
     (1,0 por defecto; configurable).
- Un cluster con efecto no evaluado (NULL) no se excluye: no saber no es un efecto. Es un límite:
  una serie corta que no se pudo evaluar puede entrar aunque su aceleración sea inflación.
- **Razones de exclusión** auditables por fenómeno (`rank_phenomena.watch_reason`): `triangulado`,
  `sin_evidencia_publica`, `sin_senal`, `sin_evidencia_de_demanda` (solo precios, tasas u otros),
  `efecto_tasas_o_inflacion`, `efecto_bajo`. Conteo en `summary.watchlist.excluded_by_reason`.
- **Datos:** columnas aditivas y nulas `watch`, `watch_reason`, `watch_effect`, `watch_q`,
  `watch_n_demand_series` en `rank_phenomena`. El validador rechaza una fila en observación que no
  sea de un solo origen, que esté publicada o que tenga `rank` o `score`.
- **Reporte:** sección «🟡 En observación» separada del ranking, con la advertencia «Un solo
  origen: no triangulado» en cada tarjeta, glosario y un tile. Solo salen ids de cluster, conteos,
  industria y mezcla de clases: no puede filtrar series con copyright.
- **Qué no hace:** no cambia el score, el ranking, la triangulación ni los detectores.

## Límites
- El umbral `min_effect` es un valor inicial; el volumen de la lista se mide en la primera corrida
  (`summary.watchlist.n_in`). Si resulta demasiado grande, se ajusta el umbral, no la regla.
- Depende de la clasificación de medida (cobertura aún no medida; Census casi todo `otro`) y de
  los efectos de tasas/inflación (aún sin corrida real).
