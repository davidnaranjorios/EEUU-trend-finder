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

**Revisión 2026-11-10 (rama `claude/efecto-tasas-nivel`).** Reporte 3914814, con TB3MS ya
descargada: `available` = true, 119 clusters, 52 evaluados y **0 marcados**, aunque la auditoría
tenía índices de T-bills y money market que deberían marcarse.
- **Causa.** El diseño regresaba el crecimiento solo sobre Δr. El crecimiento de un índice de
  acumulación (T-bills, money market, intereses devengados) es ≈ r_t / periodos por año: depende
  del **nivel**, y su cambio de pendiente es un cambio de nivel de la tasa. Tras un ciclo de alzas Δr
  vuelve a ~0 mientras el nivel se queda alto: el R² con Δr es ~0,1 (q ≈ 0,5) y el residuo conserva
  la aceleración.
- **Diseño nuevo** (`rank.rate_effect.include_level: true`): nivel contemporáneo r_t (promedio del
  periodo de la grilla) **más** Δr con rezagos 0..`max_lag` (1 + (L+1) columnas). Se eligió el nivel
  sin rezagos para no duplicar columnas casi colineales (nivel y cambio rezagados); `false` reproduce
  el diseño anterior. El residuo y la prueba de pendiente con/sin tasa quedan igual; el bootstrap
  (índices compartidos, cola GPD) y BH sobre la familia también.
- **Condición adicional, por un falso positivo medido.** Con el nivel en el diseño, una aceleración
  propia sin relación con la tasa se ajustaba por azar a su deriva (regresión espuria de dos series
  persistentes): 12 de 40 «relacionadas» y 6 de 40 marcadas con una tasa sin ciclo. Remuestrear la
  tasa en vez del crecimiento lo arregla pero deja sin potencia a la relación real (el nivel de un
  camino aleatorio ajusta cualquier rampa: p ≈ 0,002 a B = 499 incluso con R² = 0,999). Lo que
  distingue a la relación real es que sobrevive en **primeras diferencias** (g = c·r ⇒ Δg = c·Δr),
  que son estacionarias. Se exige también que el mismo modelo en diferencias sea significativo (su
  propio bootstrap de Δg y su propia familia BH; `r2_diff`, `p_diff`, `q_diff`). Medido: índice de
  acumulación con ruido realista (2·10⁻⁵ frente a una señal de ~4·10⁻³) marcado 16 de 16; tendencia
  propia 0 de 30; ruido puro 0 de 600. Límite: una serie cuyo ruido supera el cambio mensual de la
  tasa (4·10⁻⁴) no se ve en diferencias y no se marca (0 de 16).
- **Por qué no se evaluó cada cluster.** `summary.rate_effect.not_evaluated_by_reason`:
  `sin_crecimiento` (sin crecimiento preparado), `serie_corta` (menos de 2W + max(W, línea base de
  `slope`) periodos), `pocos_periodos_con_tasa`, `sin_ventana` y `sin_tasa`. Las series anuales
  quedan casi todas fuera por `serie_corta` (≤ 15 crecimientos frente a 2·7 + 33 = 47), no por el
  mínimo de filas: es la calibración de `slope` y no se cambió. `min_rows_by_grid` (M 36, Q 20,
  A 12) reemplaza el mínimo fijo de 20 filas y se puede ajustar sin tocar esa calibración.

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

## 10. Índices de precios de BEA construidos con precios de BLS (rama `claude/origen-bea-ppi`)
Corrida b23cf89a: el único candidato publicado (c7cf98c7e3ab5) triangulaba precios de
computadoras y electrónica con BEA (Table 2BUI «Implicit Price Deflators for Manufacturing and
Trade Sales»; GDPbyIndustry «Chain-Type Price Indexes for Value Added / Gross Output») y BLS (PPI
por subsector). Si BEA arma esos índices con precios de BLS, no son independientes por ORIGEN (mismo
caso que HELOC/Z.1, ADR 0010).

**Verificación en documentación oficial de BEA (2026-11-10)**
- NIPA Handbook, cap. 4 «Estimating Methods» (actualizado en diciembre de 2023), p. 4-25:
  «BEA's primary source of indexes to deflate GDP and its components is BLS, which provides detailed
  price indexes—including consumer price indexes, producer price indexes, and international price
  indexes.» Añade: «In cases where quality-adjusted indexes have not been available, BEA has
  developed its own indexes», con los ejemplos de índices hedónicos propios de computadoras (años
  80 y principios de los 90; «as BLS began publishing quality-adjusted PPIs for computers and
  peripheral equipment in the 1990s, BEA adjusted its methodologies to incorporate them»),
  semiconductores, conmutadores telefónicos digitales, software y fotocopiadoras.
  https://www.bea.gov/resources/methodologies/nipa-handbook/pdf/chapter-04.pdf
- Survey of Current Business, marzo de 1998, «Gross Product by Industry Price Measures»
  (el detalle por industria; es un documento antiguo y no hay uno equivalente reciente que haya
  podido leer): «Detailed price indexes for manufacturing and wholesale trade are primarily producer
  price indexes (PPI's) from the Bureau of Labor Statistics (BLS). Price indexes for farm products are
  from the U.S. Department of Agriculture, and price indexes for mineral products are mostly from the
  U.S. Department of Interior and the U.S. Department of Energy. Price indexes for selected
  products—including computers, semiconductors, digital telephone switching equipment, and selected
  equipment purchased by the U.S. Department of Defense—are from the national income and product
  accounts. Price indexes for retail trade and for services are primarily consumer price indexes
  (CPI's), or they are derived from the CPI's.» Para los insumos intermedios: «Detailed price
  indexes for inputs of manufactured goods are from BLS: Primarily PPI's for domestic goods and
  international price indexes for imports. These indexes are supplemented by selected price indexes
  from the NIPA's.»
  https://apps.bea.gov/scb/pdf/national/inputout/1998/0398gpo.pdf

**Qué se concluye (y qué no)**
- La fuente principal de los índices de precios y deflactores de BEA es BLS, en sus tres
  versiones (PPI, IPC y precios de importación). Por eso el origen compartido es la **agencia**
  BLS y no «PPI»: cubre las tres sin afirmar más de lo verificado.
- No es el 100 %: los índices propios de BEA (computadoras con ajuste de calidad, semiconductores,
  conmutadores, software) y los de USDA, Interior y Energía no son BLS. Se resuelve por el lado
  conservador (error de principio 4: contar una fuente de menos es menos grave que publicar un
  candidato de una sola fuente real): el índice de precios de BEA hereda **ambos** orígenes, el de
  BEA y el de BLS. Un índice de precios de BEA y una serie de BLS cuentan como una fuente; uno de BEA
  y una serie de una agencia sin relación siguen siendo dos.
- No se agregaron USDA, Interior ni Energía como orígenes: solo se verificó BLS como fuente
  principal y no hay series de esas agencias en el universo que lo justifique.

**Reglas** (`config/data_origin.yaml`; clave nueva `also_origin`, además de `inherits_from`)
- `bea_price_indexes_bls`: `source: bea`, datasets NIPA, NIUnderlyingDetail, GDPbyIndustry y
  UnderlyingGDPbyIndustry (los cuatro cuya metodología se verificó) y título de tabla con «Price
  Indexes», «Implicit Price Deflators» o «in Prices» (descripción oficial de la tabla, p. ej. T10104,
  U002BUI, GDPbyIndustry 11 y 18, UnderlyingGDPbyIndustry 215 y 225). Las tablas de cantidad
  («Chain-Type Quantity Indexes», «Chained Dollars»), de valor nominal y de otros datasets
  (Regional, ITA, FixedAssets...) no casan y conservan su agencia.
- `bea_price_indexes_bls_via_fred`: la misma regla para series que llegan vía FRED con agencia BEA
  y título «Implicit Price Deflator», «Chain-type Price Index» o «Price Indexes».
- Alcance mayor que el pedido: el encargo hablaba de índices «por industria»; el NIPA Handbook
  afirma la fuente BLS para los deflactores de GDP y sus componentes en general, así que se
  incluyeron también los índices de precios de las NIPA (PCE, inversión, exportaciones...). Si
  prefieres limitarlo a los índices por industria, basta quitar `NIPA` y `NIUnderlyingDetail`
  excepto `U002BUI` del `series_id_regex`.

**Tests** (`tests/test_origin.py`): ids y títulos reales de BEA (API consultada el 2026-11-09).
BEA-precios + BLS-PPI → 1 fuente; BEA-cantidades y valor nominal + BLS-PPI → 2 (no se sobrecorrige);
BEA-precios + Census → 2; los mismos índices vía FRED; y el esquema `also_origin`.

**Candidatos que se espera que caigan de b23cf89a** (sin acceso a los datos de la corrida; criterio
para revisarlos con la auditoría): los candidatos y fenómenos cuya evidencia de BEA sean solo
series de las tablas de precios de arriba y cuya otra fuente sea BLS. El candidato
c7cf98c7e3ab5 cumple ese criterio según la descripción del encargo (2BUI y GDPbyIndustry de precios
más PPI de BLS) y debería pasar a una sola fuente. Los fenómenos BLS+Census no cambian con esta
regla; los BEA+BLS cambian solo si todo el respaldo de BEA es de precios.

