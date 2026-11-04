# ADR 0011 — Revisión del detector de texto (corrida 2a02c981, parte C)

Fecha: 2026-11-04. Pedido de David; sin cambios a los principios no negociables, con una excepción
acotada al principio 1 (punto 3).

## Decisiones

1. **Quiebre estimado por término.** `emerging_stat_break` (`src/detect/text_filters.py`) elige el
   largo del segmento elevado L ∈ [persistencia, `recent`] que mejor ajusta un escalón de dos
   niveles (mínimos cuadrados) y calcula T en ese L. El nulo calcula lo mismo (la selección del
   quiebre está en el p). `window_start` es por término (`G − L`); `window_end` nunca pasa del
   último documento de `text_docs` (test). `recent` es solo el tope de L.
2. **Deriva del corpus.** `drift_adjust`: cada término se mide contra la mediana de cambio de los
   términos con prevalencia base parecida (`text_detect.drift`, cuantiles de la línea base). El
   estadístico y el nulo usan la serie corregida, así que una deriva global sale de ambos; sin la
   corrección, el mismo corpus sintético da decenas de señales (test de potencia del control).
   No se normaliza por longitud de documento: los conteos son de documentos, no de palabras.
3. **Especificidad.** Se excluyen unigramas con prevalencia media ≥ `unigram_max_share`, n-gramas
   sin ningún token con forma de sustantivo (heurística de sufijos: sin modelo POS, sin licencias)
   y las frases de `text_detect.boilerplate` (secuencia contigua de tokens). **Excepción aprobada
   por David el 2026-11-04** al principio 1: esa clave —y solo esa— admite una lista de frases;
   `tests/guards.py::BOILERPLATE_PATH` y `test_boilerplate_exception_is_limited_to_its_key`
   comprueban que la misma lista en cualquier otra clave sigue fallando. La prioridad de la
   entrega D favorece n-gramas de 2–3 palabras (`text_detect.priority`).
4. **Amplitud entre industrias.** `term_freq.n_industries` (SIC de 2 dígitos del emisor, desde
   `submissions`), esquema de ingesta v4; EDGAR se re-procesa (`reprocess_below_schema: 4`), las
   particiones antiguas quedan en NULL (no medido). En el score, `breadth` pasa a
   log(1 + industrias) dividido por (1 + prevalencia base / 0,05): una palabra común no gana por
   común.
5. **Vínculo término–serie.** `src/rank/comove.py`: la similitud de títulos solo SUGIERE pares. Un
   par es vínculo si el Δ logit anual del término y el crecimiento anual de la serie co-mueven
   (máx |ρ| con rezagos ±`max_lag`; nulo stationary bootstrap con índices compartidos; familia BH
   propia, q = `fdr.q`). Sin validar, la evidencia del término queda con `agencies` vacío: no es
   fuente independiente (test). `series_term_links` guarda `comove_q`, `comove_lag`, `validated`.
6. **Tasa de aprobación BH.** `detector_runs.params_json` guarda `bh_rate` y `approval_alert`
   (tasa > 3× la mediana de los demás detectores, `fdr.approval_alert`); el reporte de detectores,
   el informe HTML y el resumen del workflow la muestran.

## Entrega D

`uv run python -m src.detect.text_examples --report data/logs/detector_texto_edgar.json`: 20
términos de EDGAR con mayor prioridad tras los filtros, con 3 empresas (nombre + CIK) tomadas de
una muestra de 10-K del último periodo (se vuelven a bajar; el texto no se guarda, ADR 0005). El
workflow Detectores lo escribe en el resumen del job.

## Límites conocidos

- El vínculo usa la grilla anual; series anuales o más cortas de `min_overlap` años no se validan.
- La corrección de deriva supone que la mayoría de los términos de un grupo de prevalencia no
  emerge; con pocos términos por grupo (`min_bin`) la mediana es ruidosa.
- `n_industries` solo existirá en EDGAR tras la próxima ingesta completa.
