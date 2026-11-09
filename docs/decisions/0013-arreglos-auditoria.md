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
- (b) **NO implementado: efecto de tasas.** Requiere una serie de tasa de corto plazo como
  referencia metodológica, pero la excepción del ADR 0009 admite exactamente tres claves
  (deflactor, población, recesiones). Agregar una cuarta amplía la excepción del guard y necesita
  aprobación de David. Alternativa sin id fijo (elegir la serie de tasa por palabras clave del
  título) sería un tema escrito a mano, también prohibido. Queda pendiente.

## 6. Reauditoría (`eval/cluster_audit.py --baseline-run-id`)
Modo comparación: `comparacion.json/.md` con conteos agregados antes/después (clusters,
fenómenos, candidatos, enlaces de texto y embudo, cobertura NAICS por método, «efecto de tasas»
= desconocido). Una cifra de una corrida anterior a la función es `None`, no 0. El workflow
`auditoria-clusters.yml` recibe `baseline_run_id` (opcional).

## 8. NAICS para BEA y Census (rama `claude/naics-bea-census`)
Corrida 1567bd3a: cobertura NAICS de señales BLS 25 %, FRED 17,8 %, BEA 0 %, Census 0 %. El código
de industria no se perdía en la ingesta: ya viaja en el `series_id`.

- **BEA.** Los ids reales (verificados con la API el 2026-11-09) llevan el código de industria de
  BEA: `GDPbyIndustry:1:111CA:A`, `UnderlyingGDPbyIndustry:210:23EH:A`,
  `InputOutput:57:111CA.Industry.487OS.Commodity:A`. Método nuevo `concordancia_bea`: la tabla oficial
  de BEA («BEA Industry and Commodity Codes and NAICS Concordance», 2023-10) está versionada en
  `config/naics/bea_industry_naics.csv` (fuente, fecha y hash en `SOURCES.md`). Es NAICS **2017**:
  `scripts/build_bea_concordance.py` la pasa a NAICS 2022 con la concordancia oficial de Census y
  sube al padre cuando están todos los hijos (`111CA` → 111 y 112, `44RT` → 44-45). En InputOutput
  se ancla el lado marcado `Industry`. Los agregados sin NAICS (`PVT`, `GDP`, `31G`...) y el gobierno
  («n.a.») quedan sin fila. Las tablas Regional (SAGDP...) no llevan el código en el id y siguen por
  nombre; es la mejora pendiente más clara para BEA.
- **Census.** `codigo_en_id` lee las dimensiones `NAICS`, `NAICSaaaa`, `category_code` (EITS) e
  `industry` (QWI) del id (`col=valor`), validadas contra `naics_2022.csv`; los ceros de relleno se
  recortan, y un código de NAICS 2017 que cambió en 2022 se pasa con
  `config/naics/naics_2017_to_2022.csv` (concordancia oficial de Census, solo los 140 códigos que
  cambiaron). Valores que no son NAICS (`TOTAL`, `44X72`, `MDM`, `00`) no anclan. Límite: una
  dimensión `NAICS2012` solo se acepta si el código también es NAICS 2022 (no se encadena 2012→2017).
  Riesgo: un `category_code` numérico de una encuesta no industrial se anclaría por error; el filtro
  es que el nombre de la columna sea de industria y el valor un NAICS oficial.
- **Sin re-ingesta.** Como el código ya está en el id, basta correr `uv run python -m src.rank run`
  (o el workflow Reporte): no hay que re-ingerir metadatos de BEA ni de Census.
- Efecto esperado sobre el embudo: más señales de BEA y Census con NAICS, lo que permite que los
  clusters se agrupen en fenómenos (ADR 0013, sección 3).

