# 0008 — Detector de términos emergentes (Fase 3, parte B)

- Fecha: 2026-11-03
- Estado: propuesta implementada. Las recomendaciones A–D del plan de la parte B están a la
  espera de confirmación de David; los parámetros quedan en `text_detect` de config.yaml.

## Contexto

CLAUDE.md pide un detector de "términos emergentes": crecimiento sostenido de la proporción de
documentos que mencionan un término, con prueba sobre esa proporción, block bootstrap sobre los
periodos y BH por fuente y corrida. La entrada es `term_freq` (ADR 0005), que no guarda el texto.
El bootstrap y la FDR son los de la fase 2 (ADR 0007); no se duplican.

## Decisiones

### 1. Familia y unidad
- Unidad de prueba: (sección, término) de una fuente; `object_id = "<sección>|<término>"`.
- Una familia FDR por fuente y corrida (`edgar`, `federal_register`), con un `run_id` por fuente.
  BH decide; BY solo se reporta. Se escriben `detector_tests` (todas las pruebas),
  `signals` (las que pasan BH) y `detector_runs`, con `n_tests`, `n_bh` y `n_by`.

### 2. Grilla y censura
- **EDGAR anual**, por año de presentación: los trimestres mezclan cohortes de cierre fiscal y Q1
  concentra el ~70 % de las presentaciones del año (ADR 0005). **Federal Register trimestral.**
- Solo entran periodos completos (todos sus subperiodos con documentos, en todas las secciones).
- Un n-grama sin fila en un subperiodo tuvo menos de `text.min_doc_count` documentos. Se imputa en
  el tope (`min_doc_count − 1`): nunca infla un pasado bajo, así que es conservador para
  "crecimiento".

### 3. Prefiltro (no mira el estadístico, para no sesgar la FDR)
- Palabras funcionales desde los datos: unigramas con proporción mediana ≥
  `text_detect.max_median_share` (0,5) en al menos la mitad de los subperiodos. Se excluyen ellos
  y los n-gramas que empiezan o terminan en ellos. No hay ninguna lista escrita a mano.
- Prevalencia media sobre toda la historia ≥ `text_detect.min_mean_share` (0,002 ≈ 15
  presentaciones al año en EDGAR). Los conteos de cada paso quedan en `params_json`.

### 4. Estadístico
- x_t = logit((c + 0,5) / (n + 1)), con suavizado de Jeffreys.
- **T = mín(x en los últimos k periodos) − media(x en los W periodos previos a la ventana
  reciente R)**.
  - k es la persistencia de config: A 2, Q 3.
  - R = `text_detect.recent` (A 4, Q 8), con R ≥ k.
  - W = `text_detect.window` (A 7, Q 20).
- Solo es grande si **todos** los últimos k periodos están elevados, así que un salto aislado no
  cuenta (test). El mismo T se calcula en cada réplica, así que el p sigue siendo válido.

### 5. Nulo y p-value
- Post-blackening AR(1) (`src.stats.bootstrap.ar_null`) sobre la **historia previa a R**, con
  stationary bootstrap y **los mismos índices** para toda la familia. Todas las series de una
  fuente tienen el mismo largo, así que hay una sola matriz de índices. La longitud de bloque es
  la mediana de las óptimas de Politis-White sobre una muestra fija de hasta 5.000 términos.
- p con cola GPD (`src.stats.fdr.bootstrap_p`) y 999 réplicas.
- **Por qué la línea base excluye R:** con el inicio del crecimiento dentro de la línea base, φ
  subía de 0,2 a 0,6 y el p de un término inyectado pasaba de 1e-10 a 4e-4.
- **Margen de φ = 2 errores estándar** (`text_detect.phi_margin`), más conservador que el 1 de
  la fase 2. Medido sobre 200 familias nulas de 80 términos con un factor común (ρ = 0,5):

| Grilla | φ | margen | FDR (P(algún rechazo)) | tamaño 5 % | 1 % | 0,1 % |
|---|---|---|---|---|---|---|
| A (24 años) | 0 | 1 | 0,020 | 3,3 % | 0,9 % | 0,03 % |
| A | 0,7 | 1 | 0,070 | 5,6 % | 1,9 % | 0,18 % |
| A | 0,9 | 1 | 0,125 | 7,1 % | 2,7 % | 0,30 % |
| A | 0 | **2** | 0,010 | 1,8 % | 0,4 % | 0,01 % |
| A | 0,7 | **2** | 0,020 | 3,5 % | 0,9 % | 0,04 % |
| A | 0,9 | **2** | 0,060 | 5,2 % | 1,6 % | 0,12 % |
| Q (107 trim.) | 0,7 | 1 | 0,030 | 4,2 % | 1,0 % | 0,01 % |
| Q | 0,9 | 2 | 0,040 | 4,8 % | 1,4 % | 0,04 % |

  La grilla trimestral solo se midió sobre 100 familias.

- **Potencia en la grilla anual** (margen 2, φ = 0,7; 100 repeticiones): un aumento sostenido del
  logit en los últimos 4 años (+0,5 / +0,7 / +1,0, es decir ×1,6 / ×2 / ×2,7 en la proporción) da
  p < 0,05 en 49 % / 74 % / 95 % de los casos y p < 0,001 en 6 % / 9 % / 29 %. Con 20 años
  anuales y ruido persistente, el detector solo ve cambios grandes. Es lo esperable (BackTrend
  2026) y conviene tenerlo presente al fijar el umbral de recall de la Fase 6.

## Verificación con datos reales (2026-11-03)

Federal Register 2016-01 a 2026-10 bajado en el contenedor (130 meses, 273 MB de `term_freq`),
grilla trimestral (43 trimestres completos, 2016Q1–2026Q3), configuración por defecto:

| Paso | Cantidad |
|---|---|
| (sección, término) candidatos | 1.490.273 |
| palabras funcionales detectadas en los datos | 692 |
| excluidos por el prefiltro | 1.425.765 |
| pruebas (familia BH) | 64.508 |
| señales BH (q ≤ 0,05) | 303 (notice 214, prorule 47, rule 35, presdocu 7) |
| sobreviven también a BY | 63 |
| tiempo | 104 s (4 procesos) |

Las señales con mayor efecto mezclan dos cosas:
- **Cambios de contenido:** "deregulation", "unleashing prosperity", "rescinding",
  "american energy", "categorical exclusions", sobre todo en rule y prorule.
- **Artefactos administrativos:** nombres de firmantes de avisos ("christopher abbott",
  "mcdonough") y fórmulas de trámite ("send additional written requests").

Ambas cosas son cambios reales en los datos, pero las segundas no son tendencias estructurales.
Se quitan con datos, no con listas:
- en la Fase 5, con deduplicación y triangulación (un término sin respaldo en otra fuente no
  entra al ranking);
- **opcionalmente**, con una medida de amplitud: en cuántas entidades (agencias o empresas)
  aparece el término. Hoy `term_freq` no la tiene; agregarla cambiaría el esquema, así que queda
  propuesta para decidir.

## Uso

```
uv run python -m src.detect.text --source edgar | federal_register
```

Lee `data/processed/term_freq/<fuente>/`. Escribe `data/signals/` y
`data/processed/lineage/{detector_tests,detector_runs}/`. La corrida semanal automática queda
para la Fase 7.

## Riesgos y mejoras futuras
- Hay muchos n-gramas casi duplicados (un 3-grama y sus 2-gramas). La deduplicación en clusters
  es de la Fase 5; aquí cada uno es una prueba, y eso hace BH más estricto, no más permisivo.
- La imputación en el tope pierde potencia para términos que nacen desde cero. Un modelo con
  censura (Tobit) es la alternativa si la Fase 6 muestra que hace falta.
- BERTrend queda descartado por ahora (ADR 0006).
