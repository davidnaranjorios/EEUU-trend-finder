# 0007 — Detector numérico y FDR bajo dependencia (Fase 2)

- Fecha: 2026-11-03
- Estado: aceptada. El plan lo aprobó David el 2026-11-03 con tres decisiones: A = cola GPD,
  B = un solo job con 4 procesos, C = ventanas de 5 + 5 años (anual 7 + 7) y lookback de 15 años.

## Contexto

CLAUDE.md pide dos detectores: cambio de pendiente (Theil-Sen) y quiebres de régimen (PELT). Cada
p-value se calibra con stationary bootstrap, con los mismos bloques de tiempo para toda la familia.
Después se aplica BH, y BY se reporta solo como comparación. El universo es de 53.008 series
nacionales y la corrida tiene que caber en GitHub Actions.

## Costo medido antes del diseño

Muestra: 2.850 series reales de los mismos 14 releases de la corrida real (solo la primera
página de cada uno, API v2), en 4 vCPU, como el runner. Son 999 réplicas
con los mismos índices para toda la familia. Segundos por serie, en 1 núcleo:

| Frec. | Obs. | Theil-Sen ×999 (numpy) | CUSUM ×999 | PELT ×999 (ruptures) | PELT 1 vez (jump=5) |
|---|---|---|---|---|---|
| D | 2.100–8.500 | 0,19 (agregado a mensual) | 0,20 | 43.800 | 0,69 |
| W | 1.260–2.400 | 0,09 (agregado a mensual) | 0,02 | 3.000 | 0,66 |
| M | 190–850 | 0,055 | 0,002 | 2.700 | 0,026 |
| Q | 105–130 | 0,007 | 0,001 | 57 | 0,002 |
| A | 68 | 0,002 | 0,001 | 13 | < 0,001 |

PELT dentro del bootstrap costaría ~40.000 h-núcleo para el universo: es inviable. Por eso PELT
**solo fecha** el último régimen de las series que sobreviven a BH, y la prueba del quiebre es un
CUSUM calibrado con el mismo bootstrap.

## Decisiones

### 1. Datos y transformación (`src/stats/transform.py`)
- Se usan los valores vigentes (`vintage_date IS NULL`) de la última corrida del embudo, en los
  grupos `download.groups`, con al menos `eligibility.min_obs` observaciones.
- D y W se agregan a mensual (promedio del mes). Así quedan tres grillas (M, Q, A) y la persistencia
  de D y W se convierte a meses.
- Se trabaja con el log-crecimiento si la serie es positiva; si no, con la diferencia.
- Las series NSA* se desestacionalizan restando la media de cada mes o trimestre.
- El crecimiento se winsoriza en mediana ± 4·MAD, así un salto aislado queda acotado.
- Huecos: relleno hacia adelante, con un máximo de 10 % de la ventana.

### 2. Detectores (`src/detect/numeric.py`)
- **slope:** Theil-Sen del log-nivel en los últimos W periodos frente a los W previos (M 60,
  Q 20, A 7). La **persistencia es parte del estadístico**: si en la ventana reciente no hay una
  racha de al menos `min_consecutive_periods` del lado del cambio, T = 0. Se aplica igual en cada
  réplica, así que el p sigue siendo válido.
- **regime:** CUSUM normalizado del crecimiento sobre 15 años, con segmentos de al menos `k`
  periodos. PELT (l2, jump=5, pen = 3·log N·sd²) fecha el último régimen de los sobrevivientes.
- `effect_size` está estandarizado por la sd robusta del crecimiento: es comparable entre series
  y unidades.

### 3. Nulo bootstrap (`src/stats/bootstrap.py`)
- **Stationary bootstrap con índices compartidos.** Se usa la misma matriz de índices para todas
  las series de una subfamilia (misma grilla y largo), generada desde `fdr.calibration.seed` y la
  forma del problema. Las series se alinean por su último dato. Cualquier proceso o shard regenera
  los mismos índices.
- **Longitud de bloque automática:** Politis-White (2004) con la corrección de Patton et al.
  (2009), tomando la mediana de la subfamilia.
- **Post-blackening** (Davison & Hinkley 1997, §8.2.4):
  - Se ajusta un AR(1).
  - Se remuestrean sus residuos con los índices compartidos y se re-colorea con el AR.
  - φ se estima desde las diferencias, porque es robusto a un cambio de media.
  - Se corrige el sesgo de Kendall y se suma un margen de 1 error estándar.
- **slope** remuestrea la **historia previa** a la ventana reciente (línea base); **regime**, la
  serie completa.

Por qué no el bootstrap por bloques directo. Lo midieron los tests (`tests/test_detect_fdr.py`) y
pruebas de tamaño sobre 1.500 series nulas. Con AR(1) φ=0,7 y 10–15 años de datos, remuestrear
bloques de la serie observada subestima la cola: alargar los bloques no lo corrige (q99 0,99 frente
a 1,17 real), porque la muestra no contiene su propia variación de baja frecuencia. Tamaño medido
de slope a niveles 5 % / 1 %:

| Variante | AR(1) φ=0,7 | iid |
|---|---|---|
| Bloques sobre la ventana | FDR 0,23 en la familia de test | — |
| Post-blackening, Yule-Walker AR(p≤4) | 7,5 % / 2,0 % | 5,8 % / 1,3 % |
| Post-blackening, φ por diferencias + Kendall | 5,8 % / 1,5 % | 5,3 % / 0,9 % |
| **… + margen de 1 se (elegido)** | **3,8 % / 0,9 %** | **3,7 % / 0,5 %** |

También se probaron, y se descartaron, dos variantes con residuos del modelo con cambio (Hall &
Wilson 1991): remuestrear los residuos y estimar solo el bloque sobre ellos. Ganan potencia, pero
con AR(1) la FDR medida subió a 0,12–0,52.

### 4. p-value y FDR (`src/stats/fdr.py`)
- p empírico = (1 + #{T* ≥ T}) / (B + 1), con B = 999. Con ~53.000 pruebas, ese piso de 0,001 no
  deja descubrir nada salvo que unas 1.060 pruebas estén en el piso. Por eso se usa la **cola GPD**
  (decisión A).
- Cola GPD: Knijnenburg, Wessels, Reinders & Shmulevich (2009), "Fewer permutations, more accurate
  P-values", *Bioinformatics* 25(12), i161–i168, DOI
  [10.1093/bioinformatics/btp211](https://doi.org/10.1093/bioinformatics/btp211). Verificado el
  2026-11-03 en los metadatos de Crossref y en el abstract.
  - Se activa si hay menos de 10 excedencias.
  - Ajusta una GPD a las 250 réplicas más grandes (como máximo B/4) y baja de a 10 si el ajuste
    no pasa Cramér-von Mises.
  - Salvaguarda propia: si la forma da < 0 (cola acotada), se usa la exponencial. Sin esto, el
    nulo global daba falsos descubrimientos con p = 1e-12.
- Validación contra la opción 2 (más réplicas), en `test_gpd_tail_agrees_with_many_more_resamples`:
  en pruebas que quedan en el piso con B=999, el p de la GPD queda a menos de medio orden de
  magnitud del p empírico con B=200.000.
- **Escalación en regime (opción 2 donde es barata).** El CUSUM cuesta ~0,002 s por 999
  réplicas, y su cola es acotada: la salvaguarda exponencial deja el p en el piso (en la primera
  corrida real, 0 señales de 42.972 pruebas). Por eso las pruebas de regime con menos de 10
  excedencias se recalculan con 99.999 réplicas (`detect.regime_escalate`), con índices
  compartidos por semilla, y la GPD se aplica sobre ese nulo si aún hace falta (`*_esc` en
  `p_method`). En slope (Theil-Sen ~100× más caro) queda solo la GPD.
- Validación de la GPD con datos reales: 34 pruebas de la muestra en el piso con B=999, comparadas
  contra 99.999 réplicas (`|log10(p_GPD / p_99.999)|`, solo casos con al menos 10 excedencias en la
  referencia):

  | Detector | Casos | Mediana | p90 | GPD ≥ referencia |
  |---|---|---|---|---|
  | regime | 21 | 0,20 | 0,56 | 15 |
  | slope | 7 | 0,17 | 0,58 | 4 |

  Otros 6 casos (2 de regime, 4 de slope) siguen con menos de 10 excedencias aun con 99.999
  réplicas.
- Familias: una por detector y por corrida, con todas las frecuencias juntas. Cada señal guarda su
  `n_tests`. BH decide; BY se cuenta en `detector_runs.n_by` y en el reporte.

### 4b. Historia mínima de slope en anuales
Con lookback de 15 años, una serie anual tiene 15 crecimientos y una línea base de 8 periodos.
Tamaño medido de slope anual con AR(1) φ=0,7 (1.000 series nulas, W=7), como P(p<0,001):

| Línea base | P(p<0,001) |
|---|---|
| 8 | 2,2 % |
| 23 | 0,6 % |
| 33 | 0,1 % |

En iid, con línea base de 8, también da 0,3 %. Por eso slope exige una línea base de al menos
`max(W, detect.slope_min_baseline = 33)` periodos y, si 15 años no alcanzan, toma más historia (el
lookback de 15 años de la decisión C rige para el CUSUM). En la muestra quedan fuera 1.828 series
anuales de 18.715; 21.421 de las 22.425 anuales tienen 60 o más observaciones.
`test_size_in_quarterly_and_annual_grids` verifica el tamaño en Q (W=20, 15 años) y en A
(W=7, 40 años).

### 5. Tablas y cómputo
- `detector_tests` (`processed/lineage/detector_tests/`): una fila por serie × detector × corrida,
  con estadístico, efecto, p, método del p, q (BH), q (BY), bloque y ventana. Explica por qué una
  serie no dio señal.
- `signals` (`data/signals/`): solo los sobrevivientes de BH.
- `detector_runs` (`processed/lineage/detector_runs/`): n_tests, n_bh, n_by, q y parámetros.
- Reporte: `data/signals/reports/<fecha>_<run_id>.{json,md}`.
- CLI: `uv run python -m src.detect run [--shard I/N --run-id ID] | fdr --run-id ID | report`.
- `.github/workflows/detectores.yml`:
  - Un job con 4 procesos (spawn).
  - Trae solo `processed/series`, `processed/lineage`, `signals` y `logs`, y sincroniza solo las
    tablas que escribe.
  - Si deja de caber en 6 h: matriz de shards y un job `fdr`.

## Corrida sobre datos reales (muestra en este contenedor)

Este contenedor no tiene credenciales de R2. Se usó una muestra de 14 releases: 12 elegidos al
azar (semilla 7) más el 18 y el 21, para tener series diarias y semanales. Son 63.350 series vía API v2, con todas sus páginas. Filtros de
`config.yaml` (frecuencia, `min_obs`, inactividad, descontinuadas): quedan 50.429 series con
11,0 M de filas (A 22.425, Q 20.561, M 7.367, D 39, W 37). No pasó por el embudo completo
(variantes ni geografía, que requieren crawl-meta), así que incluye series regionales.

Corrida `83240c7a24214a83bcdbd802774535b6`: 6 min 6 s en 4 núcleos (20 min de CPU).

| Detector | Pruebas | BH | BY | Suben | Bajan | Con copyright (BH) |
|---|---|---|---|---|---|---|
| slope | 41.122 | 187 | 97 | 181 | 6 | 68 |
| regime | 42.972 | 313 | 0 | 292 | 21 | 0 |

| Detector | Frec. | Pruebas | BH | BY |
|---|---|---|---|---|
| slope | A | 16.887 | 20 | 12 |
| slope | Q | 17.341 | 108 | 78 |
| slope | M | 6.825 | 59 | 7 |
| slope | D / W | 33 / 36 | 0 / 0 | 0 / 0 |
| regime | A | 18.715 | 0 | 0 |
| regime | Q | 17.356 | 3 | 0 |
| regime | M | 6.825 | 310 | 0 |
| regime | D / W | 39 / 37 | 0 / 0 | 0 / 0 |

BY no deja ninguna señal de regime porque su p mínimo, con 99.999 réplicas, es 1e-5, y BY necesita
~1e-7 con m≈43.000. Con 53.008 series nacionales se espera un tiempo similar, de ~6–8 min. Sin
escalación tardaba 3,4 min.

## Consecuencias y límites
- El post-blackening con margen es **conservador**: con ruido iid el tamaño real es ~3,7 % a un
  nivel nominal de 5 %. Se pierde potencia a cambio de no violar el principio 3.
- La dependencia entre series se conserva **dentro de cada subfamilia** (misma grilla y largo).
  Series de distinto largo o grilla no comparten índices; BH sigue siendo válido bajo dependencia
  positiva (PRDS), y BY se reporta como cota sin supuestos.
- Las series con `copyrighted = true` se analizan y entran a `signals` con su marca en
  `detector_tests`. La Fase 5 las excluye del reporte publicado.
- Pendiente para la Fase 5: deduplicación. Hoy una misma variable en muchas series cuenta como
  muchas señales.
