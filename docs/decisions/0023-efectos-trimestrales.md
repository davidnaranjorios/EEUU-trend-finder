# ADR 0023 — Efectos de tasas e inflación: exigencia de historia como `slope`

Estado: aplicada (2026-11-11), tras medir la FDR sobre ruido (abajo). Aplica la propuesta 1 del
ADR 0018. La propuesta 2 (evaluar con la ventana del detector que generó la señal) no se toca.

## Problema

Corrida 3361573d: 127 clusters; efecto de tasas e inflación evaluaron 54 y dejaron 73 como
`serie_corta` (los 67 trimestrales de 15 años y el resto anuales/mensuales cortos). `evaluate`
exigía 2·W + max(W, línea base) periodos de crecimiento: M 180, Q 73, A 47. Los clusters llevan
15 años (M 180, Q 60, A 15), así que ninguna serie trimestral podía evaluarse.

## Cambio

`evaluate` exige lo mismo que `slope`: W + max(W, `slope_min_baseline`) (su línea base `g[:-W]` ya
contiene la ventana previa): M 120, Q 53, A 40. Código: `src/rank/rate_effect.py` (la inflación usa
la misma función). Efecto: pasan las trimestrales de 15 años (60 ≥ 53) y las mensuales de 10 a 15
años; las anuales de 15 años siguen fuera (15 < 40) y las mensuales de menos de 10 años también.

## Medición de FDR sobre ruido (antes de aplicar)

`eval/fdr_efectos_trimestrales.py`: 150 familias de 60 clusters por caso; crecimiento AR(1) con un
factor común (correlación 0,5 entre clusters), tasa independiente (cambios AR(1) φ = 0,5; nivel
AR(1) φ = 0,95). Todo lo marcado es falso positivo; se cuenta P(≥ 1 rechazo por familia), que con
todo nulo es la FDR y debe quedar ≤ q = 0,05.

| Grilla | n | φ del crecimiento | P(alguna relación: q_bh y q_diff ≤ q) | P(alguna marca `rate_effect`) |
|---|---|---|---|---|
| Q | 60 | 0,3 | 0,000 | 0,000 |
| Q | 60 | 0,7 | 0,013 | 0,000 |
| Q | 60 | 0,9 | 0,007 | 0,000 |
| Q | 53 | 0,3 | 0,000 | 0,000 |
| Q | 53 | 0,7 | 0,000 | 0,000 |
| Q | 53 | 0,9 | 0,007 | 0,000 |
| M | 130 | 0,3 | 0,013 | 0,000 |
| M | 130 | 0,7 | 0,000 | 0,000 |
| M | 130 | 0,9 | (ver abajo) | (ver abajo) |

Todo dentro de lo esperado (≤ 1,3 % frente a q = 5 %). La exigencia es conservadora porque la
etiqueta exige relación en niveles Y en diferencias, pérdida de pendiente y pendiente original
significativa. Los 150 por caso dan un error de Monte Carlo de ±2 puntos: la medición descarta una
FDR inflada, no prueba que valga exactamente 0.

## Cuántos clusters pasan a evaluarse

En la corrida 3361573d: hasta **67** (todos los trimestrales de 15 años, 60 ≥ 53) de los 73
`serie_corta`; los otros 6 (anuales o mensuales cortos) siguen sin evaluarse. Es una derivación
del conteo de la corrida y de las reglas de arriba, no un conteo medido con el código nuevo: la
corrida siguiente lo confirma con `summary.*.coverage` (ADR 0018). Los clusters nuevos se evalúan
con el mismo diseño (Δr y nivel de la tasa, mismos rezagos y mismas pruebas); no cambia el ranking
ni el score, solo etiquetas.

## Tests

`tests/test_efectos_trimestrales.py`: exigidos por grilla (M 120, Q 53, A 40), Q de 60 se evalúa y
Q de 52 / A de 15 no, M de 130 sí y de 110 no. Los de `tests/test_rate_effect.py` e
`tests/test_inflation_effect.py` siguen en verde.

## Límites

Con 53–60 trimestres la línea base del nulo de la pendiente es de 33–40 periodos: es el mínimo
que ya aceptaba `slope`. Las anuales no pueden evaluarse con 15 años; ampliar `lookback_years`
para ellas sería otra decisión.
