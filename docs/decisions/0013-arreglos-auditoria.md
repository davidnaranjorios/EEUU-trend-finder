# ADR 0013 — Arreglos de la auditoría de la corrida f9189307

Fecha: 2026-11-09. Diseño aprobado por David (encargo del 2026-11-09). Complementa ADR 0009, 0010
y 0012. No cambia los principios no negociables ni amplía ninguna excepción del guard.

## Hallazgos (muestra de 20 clusters de 119; 1 candidato, 118 de una sola fuente)
0 enlaces término–serie validados; un fenómeno partido en varios clusters; NAICS mal asignado;
independencia dudosa (Call Reports contra el agregado Z.1); ruido (series idénticas, tasas).

## 1. Embudo de enlaces texto–serie (`src/rank/run.py`, `comove.py`)
- **Causa probable principal.** Los pares sugeridos por similitud se tomaban contra las ~400 mil
  series del universo (`top_k` = 20 por término) y después solo sobrevivían los que caían en una
  serie con señal: casi todo el `top_k` se llenaba de series sin señal y se descartaba antes de la
  prueba de co-movimiento. Ahora hay un segundo pase contra las series con señal, con su propio
  umbral nulo por término (mismo cuantil y mismos términos del nulo). La tabla
  `series_term_links` se amplía con esos pares.
- **Alineación de frecuencias (ya correcta, ahora verificada por test).** La serie se agrega a
  la frecuencia anual del término (suma de crecimiento log de años completos); el término nunca
  se interpola.
- **Muestra mínima por grilla.** `rank.links.comove.min_overlap_by_grid` (M 8, Q 8, A 6). Los
  pares sin ese solape no son pruebas: no entran a la familia BH, pero se cuentan.
- **Instrumentación.** `rank_runs.params_json.summary.link_funnel` guarda los conteos por etapa:
  `pairs_similarity_universe` → `pairs_similarity_on_signal_series` → `pairs_added_signal_pass` →
  `pairs_similarity_total` → `pairs_in_cluster_universe` → `pairs_window_overlap` →
  `comove_with_data` → `comove_enough_overlap`/`comove_tested` → `comove_bh` →
  `terms_validated`. Los términos candidatos ya eran todas las señales BH de `text_emerging`.
- Los valores iniciales son hipótesis: se confirman con el embudo de la próxima corrida real.

## 2. NAICS más estricto (`src/rank/naics.py`)
- Nuevo método `codigo_en_id`: PPI de BLS (`PCU<naics><producto>`, también en FRED) y JOLTS
  (`JT[SU]<industria>…`), validado contra la tabla oficial; ceros de relleno recortados
  (524100 → 5241). Prioridad: metadatos > id > nombre.
- `nombre_oficial` exige el nombre COMPLETO, delimitado por coma, «and» o separador, y gana el
  más largo («Accommodation» ya no casa dentro de «Accommodation and Food Services»). Un título
  con varios sectores reparte el peso entre todos los que casan.
- Herencia por cluster solo con mayoría ponderada (`rank.naics.cluster_majority`, 0,5, estricto).
  `heredado_*` se marca «estimado» en el reporte.
- Sin listas escritas a mano: solo `config/naics/`. Los formatos de id son regex estructurales.
- Pendiente: otros formatos de id de BLS con código embebido (se agregan en `ID_FORMATS`).

## 3. Fenómenos (`src/rank/phenomena.py`, tabla `rank_phenomena`)
Los clusters se unen (componentes conexas) si comparten NAICS a `rank.phenomena.naics_digits`
(3; un código es compatible con otro si uno es prefijo del otro; rangos «31-33» se expanden), tienen
la misma dirección del efecto (acuerdo ≥ `min_direction_share`) y ventanas solapadas. La
triangulación del fenómeno usa `assess` sobre la evidencia unida: las fuentes se cuentan por
ORIGEN (ADR 0010) y la regla no se relaja. `by_grouping` marca los fenómenos que triangulan solo
al juntar clusters. El reporte muestra los fenómenos publicables con sus clusters (solo ids de
cluster y conteos: no puede filtrar series con copyright). `rank_candidates` no cambia.

## 4. Agregados Z.1 (`config/data_origin.yaml`)
Nueva clave opcional `inherits_from` en una regla: la serie comparte identidad con el origen de
las reglas que nombra. `z1_all_sectors_aggregate` (release 52, sector 89 en el id) hereda de
`z1_banks_call_reports`, así que HELOC de Call Reports ya no triangula contra el agregado de
todos los sectores. Otros sectores agregados de la Z.1 NO se agregaron: no se pudieron confirmar
sin acceso a los títulos reales; se revisan con la corrida real.

## 5. Ruido
- (a) `dedup_exact` (`src/detect/dedup.py`): series con los mismos valores en todas las fechas
  (misma grilla y primer periodo, ids distintos; las constantes no se unen) se prueban una sola
  vez, gana la fuente original; el linaje queda en `reports/dedup/<run>.json` (`exact_groups`).
- (b) **Efecto de tasas: implementado el 2026-11-09, tras la aprobación de David (opción A).**
  Ver la sección 7.

## 6. Reauditoría (`eval/cluster_audit.py --baseline-run-id`)
Modo comparación: `comparacion.json/.md` con conteos agregados antes/después (clusters,
fenómenos, candidatos, enlaces de texto y embudo, cobertura NAICS por método, «efecto de tasas»
= desconocido). Una cifra de una corrida anterior a la función es `None`, no 0. El workflow
`auditoria-clusters.yml` recibe `baseline_run_id` (opcional).

## 7. Efecto de tasas (rama `claude/efecto-tasas`)
**Excepción ampliada.** `context.reference_series` admite una cuarta clave, `short_rate` =
`TB3MS` (FRED, T-bill a 3 meses, mercado secundario, mensual, H.15, sin copyright). Es una
referencia metodológica como las otras tres: se descarga con ellas, no entra a detectores ni al
ranking (`reference_keys`) y el guard (`tests/guards.py`) acepta exactamente estas cuatro claves
(una quinta, o solo tres, falla). Aprobada por David el 2026-11-09; texto en CLAUDE.md, principio 1.

**Etiqueta** (`src/rank/rate_effect.py`, columnas `rate_effect`, `rate_share` en `clusters` y
`rank_candidates`, más `rate_q` y `rate_n_tests` en `rank_candidates`; NULL = no evaluado):
- (a) Regresión del crecimiento preparado de la serie representativa del cluster sobre Δ TB3MS,
  rezagos 0..`rank.rate_effect.max_lag` (2), en la grilla de la serie (la tasa se agrega sumando
  sus cambios mensuales). Estadístico R²; p por stationary bootstrap con índices de bloques
  compartidos por (grilla, largo) y cola GPD como en los detectores (sin ella el p mínimo,
  1/(B+1), impide que BH deje pasar nada con una familia de cientos de clusters); BH sobre la
  familia de clusters, que cada fila registra (`rate_n_tests`).
- (b) Se repite la prueba `slope` de los detectores sobre el residuo. Se marca solo si el cambio
  de pendiente original es significativo (p ≤ q) y el efecto cae ≥ `rank.rate_effect.min_drop`
  (0,5) o deja de ser significativo. `rate_share` = 1 − efecto residual / original, en [0, 1].
- Solo etiqueta: el reporte la muestra y no filtra ni cambia el score. `eval/cluster_audit.py`
  cuenta los clusters evaluados y marcados (`n_marked`; None = la corrida no la calculó).

**Límites.** Mira solo `slope` (un cluster con señal solo de `regime` no se marca); no se evalúa
en grilla anual (la línea base de `slope` no alcanza) ni con historia corta o sin la serie de tasa
descargada. Una tendencia que coincide en el tiempo con un ciclo de tasas no se distingue de una
causada por ella: la etiqueta avisa y la interpretación es humana. Valores iniciales (`max_lag`,
`min_drop`) por confirmar con la corrida real.

## 9. Enlaces texto–serie trimestrales para el Federal Register (rama `claude/texto-trimestral`)
Corrida 1567bd3a: embudo de enlaces 63 → 0 con señal, segundo pase +14 → ventana 14 → solape 11 →
testeados 11 → BH 0. El test de co-movimiento usaba Δ logit **anual** del término: con ~25 años de
Federal Register quedaban ~6–15 puntos y casi nada de potencia.

- **No hace falta reprocesar ni re-ingerir.** `term_freq` del Federal Register ya guarda **meses**
  (`2024-05`) y el de EDGAR **trimestres** (`2024Q1`) (ADR 0005); el detector de texto ya los agrega
  a su grilla. Solo la agregación del test de co-movimiento era anual. Sin costo extra de Actions
  salvo una segunda consulta de agregación sobre `term_freq` de los términos con señal.
- **`rank.links.comove.text_grid: {federal_register: Q, edgar: A}`.** El término se agrega a su
  grilla (trimestres completos: 3 meses; años completos: 4 trimestres) y **nunca se interpola**. Cada
  par se prueba en la grilla MÁS GRUESA entre la del término y la nativa de la serie: un término
  trimestral con una serie mensual o trimestral se prueba en trimestral; con una serie anual, en
  anual (el término se agrega a años). EDGAR sigue anual (10-K anuales).
- **Parámetros por grilla de comparación:** `min_overlap_by_grid: {A: 6, Q: 24}` (antes estaba
  indexado por la grilla nativa de la serie y valía 8 para M y Q; ahora un par M/Q anual pide 6, lo
  que lo hace algo menos exigente en solape pero con la misma calibración bootstrap) y
  `max_lag: {A: 2, Q: 4}`.
- **Estadística.** Mismo estadístico (máx. |ρ| sobre rezagos entre Δ logit del término y el
  crecimiento de la serie) y mismo stationary bootstrap por bloques, con índices compartidos por
  grilla y largo de eje. Una sola familia BH sobre todos los pares (trimestrales y anuales juntos);
  cada fila registra el tamaño de la familia completa (`comove_joint`).
- **Medición (tests sintéticos).** Término trimestral ligado a una serie mensual con relación 0,5:
  trimestral detecta 9 de 12, anual 2 de 12; mediana de p 0,001 frente a 0,13. Con relación 0 sobre
  ruido puro, la tasa de descubrimientos queda bajo q. Las cotas de los tests dejan margen sobre
  esas mediciones.
- **Embudo esperado.** Con ~100 trimestres por término (frente a ~25 años) los pares con términos
  del Federal Register pasan de tener ~6–15 puntos a ~24–100, así que `comove_enough_overlap` y
  `comove_tested` deberían subir y `comove_bh` dejar de ser 0 donde haya relación real. Nuevas
  claves en `link_funnel`: `comove_tested_grid_Q` y `comove_tested_grid_A`.
- Los términos de EDGAR no cambian de grilla; no se esperan cambios en sus enlaces.
