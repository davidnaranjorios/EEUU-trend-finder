# 0007 — Fuentes adicionales: BLS, BEA y Census sobre una capa común

- Fecha: 2026-11-03
- Estado: aceptada (plan aprobado por el responsable del proyecto el 2026-11-03; linaje
  "opción B" elegido el mismo día a la vista de la medición de BLS)

## Contexto

La Fase 4 agrega tres agencias para que la triangulación (principio 4) tenga más de una fuente
numérica. Restricciones: el esquema común de `src/schemas.py`, el embudo de elegibilidad con
linaje por serie (ADR 0002), GitHub Actions con estado en R2 (ADR 0004) y cero temas escritos a
mano (principio 1).

## Límites verificados (2026-11-03)

| Fuente | Límite | Cómo se verificó |
|---|---|---|
| BLS API v2 (con key) | 500 consultas/día, 50 series/consulta, 20 años/consulta | bls.gov/developers/api_faqs.htm |
| BLS masivo (download.bls.gov/pub/time.series) | Sin cupo publicado; responde 403 a clientes sin contacto en el User-Agent. ETag y Last-Modified por archivo; ~18 MB/s medidos | listado y HEAD en vivo |
| BEA | 100 solicitudes/min, 100 MB/min, 30 errores/min; al exceder, HTTP 429 con Retry-After (~60 s). Algunos errores llegan dentro de un 200 | BEA API User Guide (20-abr-2026), p. 5–6 |
| Census | Con key no hay cupo numérico publicado; los términos permiten bloquear a quien exceda o eluda límites | api.census.gov/data.json; Terms of Service |

## Decisiones

### 1. BLS por archivos masivos, no por la API v2

Con la API caben 25.000 series por día y 20 años por consulta: una sola encuesta grande ya
necesita días. Los archivos masivos traen el catálogo (`<s>.series` y tablas de códigos) y todos
los valores (`<s>.data.*`). Se leen en streaming, filtrando a las series objetivo, y nunca se
guardan completos en el runner. El User-Agent con contacto se toma del secret `SEC_USER_AGENT`
(el mismo de EDGAR; decisión del 2026-11-03). `BLS_API_KEY` queda sin usar.

### 2. Capa común (`src/ingest/common.py`) y un adaptador por fuente

Cada fuente implementa solo `SourceAdapter`: `list_units` (unidades de descarga: encuesta BLS,
tabla BEA, dataset Census), `catalog_rows` (catálogo neutral), `values_version` y
`fetch_values`. Lo demás es común:

- **Catálogo neutral** `_internal/source_catalog/<fuente>/`: título, frecuencia del esquema,
  unidades, ajuste estacional (SA/NSA...), fechas, nivel geográfico, unidad.
- **Embudo neutral.** `src/eligibility.py` se separó en `row_rule` (descontinuada, frecuencia,
  sin fechas, inactiva: miran una fila) y `classify_rows` (variantes, geografía, historia
  corta). FRED traduce sus tags a ese catálogo y da exactamente el mismo resultado (los tests
  del embudo de FRED no cambiaron). Cada adaptador traduce su propia geografía.
- **Crawl con memoria acotada.** Las reglas de fila se aplican en streaming; lo que sigue en el
  embudo se reparte por título normalizado (la regla de variantes solo compara títulos iguales)
  y, si la unidad es grande, se derrama a disco por fragmentos. Se escribe en lotes de
  `write_batch_rows` para no multiplicar archivos.
- **Incremental y reanudable.** Estado por unidad (`_internal/source_unit_state`): versión del
  catálogo y de los valores. Una unidad se reprocesa si no tiene estado, si cambió de versión o
  si su clasificación tiene más de `catalog_refresh_days`. `crawl`, `download` y `update`
  aceptan un presupuesto en minutos; lo pendiente devuelve el código 10 y sigue la semana
  siguiente.
- **Valores vigentes**, `vintage_date = NULL` (decisión 5 del plan): el backtest sigue usando
  solo ALFRED. `copyrighted = false` (dominio público federal).
- **Solo se descarga el grupo `nacional`** (`download.groups`, decisión 2); el resto queda
  clasificado con linaje y sin valores.

### 3. Linaje: una fila por serie, salvo unidades enteras excluidas por la misma regla de fila

Medición del 2026-11-03 (streaming completo de los `.series`): el catálogo de BLS suma unos
**183 M de series**. Las encuestas `ca` (52,4 M), `cb` (53,1 M), `cs` (29,0 M), `ch` (20,2 M),
`nw` (12,5 M) y `fw` (4,8 M) no tienen ningún dato desde 2025: el 100 % cae en "inactiva". Solo
436 mil series activas tienen 10 años o más de historia, en 28 encuestas.

Una fila de linaje por serie habría escrito ~180 M filas casi todas iguales. Decisión del
responsable (opción B): cuando **todas** las series de una unidad caen por la **misma regla de
fila** y son al menos `lineage_aggregate_min_series`, se escribe una sola fila con
`series_id = <unidad>/*` y `detail = n_series=<n>;max=<detalle máximo>;version=<ETag>`. El resto
lleva una fila por serie. `expand_aggregated` reconstruye desde la unidad versionada
exactamente las decisiones por serie (test), y falla si el archivo ya no es esa versión. Los
conteos del embudo (`estimate`, `catalog summary`) cuentan cada fila agregada por su `n_series`.

### 4. Corrida semanal

`infra/sources_update.sh` corre después de FRED en el mismo job (`run_actions.sh`): por fuente,
`update --budget-min $SOURCES_BUDGET_MIN` (60 por defecto) y sync a R2 **solo de sus rutas**.
Una fuente que falla no detiene a las demás; el script termina en error al final. Los secrets
`SEC_USER_AGENT`, `BEA_API_KEY` y `CENSUS_API_KEY` se pasan al job; si falta uno, solo falla su
fuente (aviso en "Verificar secrets").

### 5. Cortes de conexión

`http.stream` reintenta solo el inicio de una respuesta. En el crawl real, download.bls.gov cortó
la conexión a mitad de `ca.series` (`Connection reset`) y la fuente entera terminó en error. La
capa común repite la unidad (hasta `max_retries`; las escrituras son upserts, así que es
idempotente) y, si sigue fallando, la deja pendiente (código 10) y sigue con las demás.

## Medición real del embudo (crawl de solo catálogo, 2026-11-03)

Corrido en el contenedor de desarrollo, sin descargar valores. Grupos según la última decisión
de cada serie; las filas agregadas cuentan por su `n_series`.

| Fuente | Unidades | Series | nacional | regional | internacional | nuevas | excluida |
|---|---|---|---|---|---|---|---|
| BLS | 66 de 66 | 183.746.187 | 299.620 | 60.342 | 0 | 6.188.387 | 177.197.838 |
| BEA | 1.890 de 2.014 | 111.633 | 50.957 | 0 | 29.359 | 3.636 | 27.681 |
| Census | 89 de 89 (45 sin series, ver abajo) | 584.286 | 5.157 | 0 | 134.851 | 890 | 443.388 |

- Exclusiones por regla. BLS: inactiva 177.071.375, variante 111.790, frecuencia no soportada
  14.661, sin fechas 12. BEA: inactiva 13.540, variante 11.971, sin fechas 2.170. Census:
  inactiva 412.453, sin fechas 29.571, variante 1.364.
- Tamaño estimado del grupo `nacional` (lo único que se descarga): BLS 290,7 MB, BEA 74,2 MB,
  Census 13,3 MB de Parquet.
- Tiempos: BLS 57 min para 61 encuestas (más 5 de un intento anterior); BEA ~3 h para 1.890
  tablas (límite de 100 solicitudes/min); Census ~2 h 50 min en dos intentos. Con 60 min por
  fuente y semana, el primer crawl de BEA y Census toma varias corridas, y el refresco a 30 días
  de BEA ocupa buena parte del presupuesto: si estorba a la descarga, subir
  `catalog_refresh_days` o `SOURCES_BUDGET_MIN`.
- BLS: 13 encuestas quedan en una fila agregada cada una; el resto escribe ~7,1 M filas de linaje
  por crawl completo. Las 6,2 M "nuevas" son casi todas de `oe` (anual, historia corta).
- Census: de los 89 datasets, 45 no producen series en esta corrida: por tamaño (`excede_series`
  / `excede_filas`, p. ej. `bds` 775.700 series), sin variable de tiempo o de valor, `qwi` que
  exige un estado concreto, o timeouts/HTTP 524 del servidor en `intltrade` (transitorios: se
  reintentan en la corrida siguiente). Por eso el grupo `nacional` de Census es pequeño.

## Primera corrida en Actions (run 41695786848, 2026-11-03)

Terminó en verde, sin errores en las tres fuentes, pero el crawl usó los 60 min de cada una y
la descarga no bajó nada: BLS clasificó 26 de 66 encuestas, BEA 474 de 2.014 tablas y Census 50
de 89 datasets. Tres ajustes:

- `update` reserva presupuesto para la descarga: cuando ya hay series objetivo, el crawl usa a
  lo sumo `crawl_budget_share` (0,5) del presupuesto. Sin objetivos aún, usa todo.
- `download` mira el presupuesto **antes** de `values_version`: en BEA esa versión es una
  consulta de datos, y la corrida gastó 12 min en 436 unidades que igual quedaron pendientes.
- BEA pasa a `catalog_refresh_days: 90`. Un crawl completo de BEA toma ~4,5 h; con 30 días y
  60 min por semana no cabe, y la fuente recorrería el catálogo sin descargar nunca.

## Solape con FRED

Muchas series de BLS, BEA y Census también están en FRED. No son fuentes independientes para
la triangulación. En esta fase no se filtra nada (decisión 4 del plan): la Fase 5 agrupa el
mismo dato de dos fuentes en un cluster y lo cuenta como una sola agencia.

## Consecuencias

- El primer crawl de BLS baja ~16 GB de `.series` (~15 min a 18 MB/s) y puede tomar más de una
  corrida: el presupuesto por fuente lo reparte.
- `catalog summary` informa el embudo por fuente (última decisión de cada serie) y las filas por
  fuente, sin cambios en "Verificar R2".
- Los exportes (`src/export.py`) siguen siendo solo de FRED; agregar las fichas de estas fuentes
  queda para la Fase 5.
