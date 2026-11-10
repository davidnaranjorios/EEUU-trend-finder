# ADR 0017 — Series jóvenes: `relative_growth` y nacimiento de series

Fecha: 2026-11-10. Aprobada por David el mismo día, con estas condiciones: familia FDR propia, sin
tocar la calibración de `slope` ni de `regime`; las series jóvenes pasan SOLO a este detector (con
test); linaje documentado; y si el FDR sobre ruido no cuadra, detenerse y reportarlo antes de abrir
el PR (ver «Medición»).

## Problema
`slope` exige 5 + 5 años con una línea base mínima y `regime` historia suficiente: las series de
3 a 15 años (muchas, y justo las de industrias nuevas) no llegan. El embudo ya las separa en el
grupo `nuevas` (historia < `min_history_years`), pero no se descargaban ni se analizaban.

## Decisión

### (a) Detector `relative_growth` (`src/detect/relative.py`, `parents.py`, `young.py`)
- **Qué compara:** el crecimiento logarítmico de la serie joven con el de su **agregado padre**:
  d_t = g_serie − g_padre en el periodo común, en la grilla de la joven (mismo crecimiento
  preparado que los demás detectores: desestacionalizado y winsorizado).
- **Padre, solo de metadatos y tablas oficiales** (sin mapeos escritos a mano; el guard del
  principio 1 no cambia): misma unidad de catálogo (`category_path`: encuesta de BLS, tabla de
  BEA, dataset de Census, categoría de FRED); misma frecuencia, unidades y ajuste estacional;
  mismo título una vez quitado el nombre oficial NAICS de la propia serie (queda la medida y la
  geografía). Luego `naics_prefijo` (la serie cuyo código NAICS es el prefijo propio más largo; los
  rangos «31-33» cubren sus sectores) o, si no hay, `total_encuesta` (la serie de la misma medida
  sin anclaje de industria, solo si es única). Sin padre no se prueba, con motivo (`sin_naics`,
  `sin_padre`, `padre_ambiguo`, `grilla_distinta`, `crecimiento_no_preparable`,
  `pocos_periodos_comunes`).
- **Estadístico:** T = |media(d)| si la racha más larga de d por encima de |media|/2 (con el signo
  de la media) dura ≥ k periodos (k = persistencia de la frecuencia); T = 0 si no. La persistencia
  está dentro del estadístico, como en `slope`: un salto aislado da T = 0 y p = 1.
- **Nulo:** «la serie crece como su padre» (E[d] = 0): stationary bootstrap de d centrada con
  post-blackening AR(1), MISMOS índices de bloques para todas las parejas de una (grilla, largo),
  cola GPD para el piso del p. `effect_size` = media(d) / MAD de d.
- **Familia FDR propia:** una por corrida (`detector_tests.detector = "relative_growth"`), BH decide
  y BY compara; `finalize` la cierra aparte. No toca `detect.detectors` (slope, regime): su
  calibración, sus familias y el componente `agreement` del score no cambian.
- **Confianza por defecto amarilla (🟡)** (`detect.relative_growth.confidence`): el reporte muestra
  el detector como «serie joven, confianza 🟡».
- **Periodos mínimos** en común con el padre (`min_periods`): M 36 (3 años), Q 24 (6), A 12. Una
  serie anual o trimestral más corta se registra como `pocos_periodos_comunes`: con menos periodos
  el nulo no se calibra.

### Elegibilidad: solo a este detector (`eligibility.young`)
- `eligibility.young.groups: [nuevas]`, `min_years: 3`, `max_years: 15`.
- **Descarga:** FRED y las fuentes de la Fase 4 descargan también las series jóvenes cuyo nivel
  geográfico está en `download.groups` (hoy nacional; `config.in_young_scope`). `download.groups`
  no cambia: vintages, explorador y exportes siguen igual.
- **Detección:** `load_series` (que alimenta slope y regime) nunca las ve (su grupo no está en
  `download.groups`); `load_young_series` las carga, solo con historia entre `min_years` y
  `max_years` (una serie más vieja que la ventana no entra y va a slope y regime). Tests:
  `test_young_series_reach_only_the_relative_growth_detector` y
  `test_older_than_the_window_regional_and_non_young_groups_do_not_enter`.

### (b) Nacimiento de series (`src/detect/births.py`) — descriptivo
- Por (agencia, NAICS de `births.naics_digits` dígitos), cuenta las series cuya `start_date` cae en
  cada año calendario y prueba si los últimos `recent_years` son inusuales frente a la historia
  (más series nuevas, o menos). Una agencia que empieza a publicar muchos códigos de una industria
  es una pista; **no es una señal**: no entra a `signals`, ni al ranking, ni al score.
- Estadístico |t| de la media reciente frente a la previa; nulo desde la línea base (años previos),
  stationary bootstrap con post-blackening e índices compartidos por largo; BH sobre todos los
  grupos (una familia por corrida). Resultado en `report["births"]` del reporte de la corrida
  (`signals/reports/`); un fallo se registra ahí y no tumba las familias.
- Límite: solo se ven las series presentes en `series_meta`; la agencia es la fuente del adaptador
  (FRED agrega a otras agencias).

### Linaje
- La decisión del embudo no cambia (`eligibility`: grupo `nuevas`, regla `historia_corta`).
- Por serie joven: `signals/reports/relative_growth/<run_id>.json` con el padre elegido, cómo
  (`naics_prefijo` o `total_encuesta`), los NAICS de ambos y los periodos comparados, o el motivo
  de no probarla. Resumen en `report["relative_growth"]`.

## Medición (condición «si el FDR sobre ruido no cuadra, detenerse»)
Familias de 80–150 parejas de ruido puro, AR(1) con φ ∈ {0,3; 0,7; 0,9}, con y sin correlación
entre series (factor común, ρ = 0,5), 40 familias por celda (999 réplicas el piso). FDR bajo el
nulo completo = P(al menos un rechazo BH con q = 0,05):

| φ_margin (post-blackening) | φ=0,3 | φ=0,7 | φ=0,9 |
|---|---|---|---|
| 3 (compuerta nueva) | 0–0,05 | 0–0,17 | 0,20–0,62 |
| 4 (compuerta anterior) | no medido | ≤ 0,03 | 0,05–0,25 |
| **5 (compuerta nueva, elegido)** | **0** | **≤ 0,03** | **≤ 0,07** (anual de 12 periodos: 0,07 y 0,05) |

Con margen 3 el detector rechazaba de más en series cortas con persistencia alta, así que el
margen se subió a 5 (`detect.relative_growth.phi_margin`). Una primera versión de la persistencia
(racha de d > 0) era demasiado débil: ruido simétrico la cumple por azar; se endureció a
d > |media|/2 y se volvió a medir (tabla de arriba).

**Costo: potencia baja con series muy cortas.** Con margen 4 (potencia medida con la compuerta
anterior; con margen 5 es menor), una brecha constante de 2 desviaciones de d detecta el 42 % a 3
años mensuales (36 periodos), 17 % a 6 años trimestrales y 0 % a 12 anuales; con φ = 0,5 el 7 %,
0 % y 0 %. Es lo que cuesta no declarar tendencias donde hay 3 años de datos: con margen 5 solo
brechas grandes frente al ruido (≈ 5 desviaciones o más; «3 veces el padre» con padre suave) salen
como señal. Además el p de una brecha cercana al límite depende de φ̂ (se acerca al tope 0,98 por
azar): un resultado limítrofe no es estable. Si la potencia resulta insuficiente en la primera
corrida real, se ajusta `phi_margin` con la medición delante, no la regla.

Nacimientos (conteos Poisson con tasa log-AR(1), 60 grupos, 24 familias): con margen 3, FDR ≤ 0,04
(φ ≤ 0,5) y ≤ 0,08 (φ = 0,8); potencia 0,8–0,9 para +20 series/año durante 3 años.

## Límites
- Aún sin corrida real: cuántas jóvenes tienen padre, cuántas se prueban y cuántas salen como
  señal se verá en `report["relative_growth"]` tras la primera corrida (el coordinador la lanza
  después del merge, con `download` ya ampliado a las series jóvenes).
- El padre depende de que el nombre oficial NAICS aparezca en el título y de las unidades de
  catálogo; si no, la serie queda sin padre (con motivo) y no se inventa uno.
- El conteo de nacimientos ve solo las series presentes en `series_meta`.
- BH sigue siendo el único criterio que decide; BY se reporta como comparación.
