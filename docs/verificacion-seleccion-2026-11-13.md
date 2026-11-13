# Verificación de las condiciones de selección — 2026-11-13

Corrida auditada: ranking `38942660a49941c5ae25f9ac626b13c5` (2026-11-11), detectores
`d09cc19222c348539989a3d6dff3c654`: 319 señales `slope` de 95.499 pruebas (BH, q = 0,05).
Datos leídos de R2. Las clases de medida son las de `config/measure_kind.yaml`, con la corrección
de nómina aplicada a mano para este análisis.

Método descriptivo (no cambia ningún resultado del sistema). Para cada serie con señal se
comparan dos crecimientos anuales compuestos:
- **2014→2019:** tendencia previa al choque;
- **2019→hoy:** crecimiento sin contar el pozo de 2020–21.

Además se compara el último valor con el máximo de 2018-01 a 2020-02.

## Condiciones vigentes

| # | Condición | Dónde | ¿Se cumple bien? |
|---|---|---|---|
| 1 | Señal `slope`: 60 meses recientes contra los 60 previos (Q 20, A 7), con persistencia, bootstrap por bloques y BH | `detect.slope_window`, ADR 0007 | ⚠️ La ventana previa (2016-11 a 2021-10) **contiene el choque del COVID** (hallazgo 1) |
| 2 | Dirección «sube» = cambio de pendiente positivo | `rank/phenomena.py` | ⚠️ También cuenta «cae más despacio» (hallazgo 2) |
| 3 | ≥ 2 orígenes independientes | `rank.min_sources`, `config/data_origin.yaml` | ❌ Falla en el candidato 1 (hallazgo 3) |
| 4 | Fenómeno = clusters con NAICS compartido, misma dirección y ventanas solapadas | `rank.phenomena` | ❌ El NAICS por título une cosas distintas (hallazgo 4) |
| 5 | Evidencia de demanda (cantidad o valor real) | solo `single_origin` (ADR 0016/0018) | ⚠️ **No se exige al ranking final** (hallazgo 5) |
| 6 | Clase de medida | `config/measure_kind.yaml` | ❌ Nómina clasificada como cantidad (en arreglo, PR del Bloque 2) |

## Hallazgos

**1. Rebotes del COVID cuentan como aceleración.** El estadístico compara la pendiente reciente con la
previa, y la previa incluye la caída de 2020. Así, un regreso al nivel anterior aparece como
aceleración. De las 285 señales al alza, 22 están **todavía por debajo** de su máximo de 2018–2020,
entre ellas el empleo de residencias de enfermería (CES6562310001).

**2. «Sube» incluye caídas más lentas.** Entre las 32 señales al alza de **cantidad**:

| Tipo (2019→hoy frente a 2014→2019) | Series |
|---|---|
| Acelera de verdad | **4** |
| Gira de caída a alza | 12 |
| Sigue cayendo, pero más despacio | 13 |
| No acelera | 2 |
| Sin dato | 1 |

Ejemplo: préstamos HELOC, −6,3 %/año en 2014–19 y −1,9 %/año ahora; el sistema los marca como «sube».

Por clase, de las 285 al alza: **166 son precios** (143 aceleran: es el periodo de inflación
2021–23), 64 son valor nominal y solo 32 son cantidad.

**3. Falsa triangulación en el candidato 1.** El candidato 1 (NAICS 22) son 23 índices de precio
de vivienda (CPI de alquiler y PCE de vivienda). La segunda «fuente independiente» es la
**mediana del CPI del Fed de Cleveland** (MEDCPIM094SFRBCLE), que se calcula con los
componentes del CPI de BLS: **es el mismo origen**. `config/data_origin.yaml` no la registra, y el
detector de duplicados no la capta porque no es una copia nivel a nivel.

**4. NAICS por título une fenómenos distintos.** Títulos como «Housing and utilities» o
«Housing, utilities, and fuels» reciben el NAICS 22 (servicios públicos). El fenómeno junta así
la inflación de alquileres con el empleo en generación eléctrica (CES4422111001, CES4422000001),
que es otro fenómeno.

**5. El ranking final no exige demanda.** El candidato 1 no tiene ninguna serie de cantidad ni de
valor real y aun así es el puesto 1. La lista «en observación» sí lo exige; el ranking principal, no.

## Resultado sobre los dos candidatos de la semana

| Candidato | Qué es en realidad | ¿Se sostiene? |
|---|---|---|
| 1 · NAICS 22 | Inflación de vivienda (solo precios), triangulada con un derivado del mismo CPI | **No** |
| 2 · NAICS 6231/623 | Ingresos nominales que suben + empleo que se recupera sin superar 2019 | **Parcial**: ingreso real +22 % desde 2021; cantidad sin máximo nuevo (ver ficha 001) |

## Arreglos propuestos (requieren decisión de David)

| # | Arreglo | Toca | Costo |
|---|---|---|---|
| A | Etiqueta de trayectoria, calculada de los datos: `acelera` / `gira_al_alza` / `frena_caida` / `rebote_bajo_pico`. «Frena caída» deja de contar como «sube». | `rank/phenomena.py` (solo etiqueta + dirección) | Bajo |
| B | La pendiente previa de `slope` excluye los meses de recesión (USREC, ya serie de referencia aprobada) o se mide contra la tendencia previa al choque. | Detector y su calibración FDR (ADR 0007): hay que **volver a medir el FDR** sobre ruido AR(1) | Medio |
| C | El ranking final exige evidencia de demanda (cantidad o valor real), como la lista en observación. | Regla de selección, CLAUDE.md principio 4 / ADR 0016 | Bajo |
| D | Registrar la mediana y la media recortada del CPI del Fed de Cleveland como origen BLS CPI en `config/data_origin.yaml`. | Tabla de origen (ADR 0010) | Bajo |
| E | NAICS por título: exigir que el segmento coincida con el nombre oficial completo, no con una palabra dentro de una enumeración («Housing **and** utilities»). | `rank/naics.py` (ADR 0012) | Bajo |

Recomendación: A, C, D y E juntos en una sesión corta. B después, con su medición de FDR,
porque cambia la calibración del detector.

## Reproducir

Scripts de esta verificación: comparan crecimientos 2014→2019 y 2019→último dato por serie con
señal, con la última vintage de cada fecha (`data/processed/series` y `data/signals` de R2). La
consulta clave es:

```sql
select source, series_id,
  pow(avg(value) filter (where year(date)=2019) / avg(value) filter (where year(date)=2014), 1/5.0) - 1 as g_pre,
  max(value) filter (where date between '2018-01-01' and '2020-02-29') as pre_max,
  arg_max(value, date) as last
from v group by all
```
