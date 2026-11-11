# ADR 0021 — Detector de texto: artefactos de formato en el Federal Register

Estado: aceptada (2026-11-11). Sin cambios en `text_detect.boilerplate` ni listas de temas.

## Problema

La corrida del Federal Register marcó 204 señales BH, todas con quiebre en 2024
(`artifact_suspect: true`, `approval_alert`), con términos de apariencia de formato
("text or adobe", "free internet access", "adobe portable document", "table of appendix").

## Diagnóstico (reconstrucción 2016–2026: 229 BH, todas con `window_start` 2024-10-01)

1. **No hubo cambio de plantilla en 2024.** Los términos "de formato" son históricos (2016–2018,
   1–2 entidades); filas por documento y días muestreados son estables.
2. **El quiebre no era estimable.** Con persistencia == `recent` (8 trimestres) el largo del
   segmento solo puede valer `recent`: toda ventana empieza en G−8 por construcción, y la bandera
   `artifact_suspect` se levantaba siempre.
3. **Casi todos los términos probados estaban censurados en el piso** (conteo imputado = mín−1).
   Se mueven solo con el denominador de su sección.
4. **`drift_adjust` mezclaba secciones** en los mismos contenedores de prevalencia: la mediana de
   `notice` (denominador que cae) se restaba a términos de `rule`, que parecían crecer ≈ +1 logit
   (88 señales con efectos idénticos 0,81–0,89).
5. **131 señales `presdocu` eran series constantes** con T = 2e-16 (ruido de punto flotante) y
   nulo de varianza cero: p en el piso.
6. El filtro de amplitud no excluía términos sin ninguna fila en la ventana reciente ("sin filas"
   se leía como "sin medida").

## Cambios (criterio estadístico, sin listas)

- `drift_adjust_by_group`: la deriva se estima dentro de cada sección (`drift.by_section`).
- `term_test`: T ≤ 1e-9 o línea base sin variación → p = 1 (`MIN_STAT`).
- `load_matrix(recent_rows_periods=persistencia)`: se excluyen términos sin filas en los últimos
  `persistencia` periodos (`require_recent_rows`); se reporta `excluidos_sin_filas_recientes`.
- `break_year_histogram(estimable=recent > persistence)`: sin quiebre estimable la bandera no se
  levanta y el reporte dice `break_estimable: false`.

## Medición

Misma reconstrucción real: 229 BH → **0 BH** (123 pruebas; 4.971 términos excluidos por falta de
filas recientes; `artifact_suspect` false, `break_estimable` false). Los tests sintéticos
existentes (término inyectado se detecta, salto aislado no) siguen en verde.

## Tests

`tests/test_text_formato.py`: serie constante con ruido de flotante → p = 1; deriva por sección
sin fuga; salto de plantilla en un periodo no es señal y un crecimiento sostenido sí; bandera de
quiebre no estimable.

## Límites

Que ahora no haya señales en FR no prueba que no haya términos emergentes: con la censura de la
ingesta la mayoría de términos nunca muestra filas. Mejorar la sensibilidad requiere conservar
conteos bajo el mínimo (decisión de ingesta, no tomada aquí).
