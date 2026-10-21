# 0002 — Almacenamiento en R2, vintages por muestra y linaje con run_id

- Fecha: 2026-10-21
- Estado: propuesta (pendiente de que se fijen los umbrales del embudo)

## Contexto

Con la API v1, bajar los valores de las 301.036 series que pasaban el filtro de la Fase 1
tomaba ~60 h (84 series/min medidas), y pedir el historial ALFRED completo de cada serie
agregaba dos problemas: el tope de 2.000 vintages por consulta y las series que no existen
en ALFRED. Además, el sistema necesita poder explicar por qué cada serie entró o no al
análisis, y alimentar una futura web estática sin consultar Parquet desde el navegador.

## Decisiones

### 1. Parquet en Cloudflare R2, consultado con DuckDB vía httpfs

- El VPS escribe Parquet local con el layout de `src/ingest/store.py`
  (`<tabla>/<fuente>/<NN>/part-*.parquet`) y lo sincroniza a R2 al cerrar cada tramo
  (`infra/sync_r2.sh`, `rclone sync`). R2 es el almacenamiento definitivo; el volumen del
  VPS es de trabajo.
- Lectura remota: `src/catalog.py::connect_r2(cfg["storage"])` crea un secreto `TYPE r2` y
  las mismas vistas que `connect()` (`series`, `series_meta`, `eligibility`) sobre
  `r2://<bucket>/<prefix>/...`. Así cualquier máquina con las credenciales consulta sin copiar.
- Configuración en `config.yaml` → `storage.bucket`, `storage.prefix`. Credenciales solo en
  `.env`, con los nombres estándar de S3 (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) más
  `CLOUDFLARE_ACCOUNT_ID`, que leen rclone y DuckDB.
- Protecciones del sync:
  - se niega a correr si `data/processed` no tiene Parquet (volumen sin montar);
  - lo que se borra en R2 va a `_trash/<fecha>/`, porque `store.py` reemplaza archivos al
    reescribir un bucket.

### 2. Valores vigentes para el universo; vintages de ALFRED solo para una muestra

- **Universo (grupos de `download.groups`):** se guardan los valores vigentes con
  `vintage_date = NULL` ("no aplica", como permite el esquema común). Una revisión reemplaza
  el valor (`upsert` la cuenta en `rows_replaced`). Esto elimina el tope de vintages y el error
  "no existe en ALFRED" fuera de la muestra.
- **Muestra (`vintages`):** aleatoria y estratificada por frecuencia × agencia (la agencia sale
  de `release/sources`).
  - Asignación proporcional, con al menos una serie por estrato; semilla `vintages.seed`;
    tamaño `vintages.sample_size` sobre los grupos `vintages.groups`.
  - Se sortea una vez y queda fija en `_internal/fred_vintage_sample`, para que el backtest
    no cambie de muestra entre corridas.
- En la muestra se guardan **todas** las filas realtime, no solo las vigentes. El código de la
  Fase 1 descartaba las superadas, y así no se puede reconstruir "lo que se sabía" en una fecha
  (principio 5). `vintage_date = realtime_start`; la vista "tal como se conocía en t" es la fila
  de mayor `vintage_date <= t` para cada fecha.
- **Tope de 2.000 vintages:** se piden las fechas con `series/vintagedates` y el rango realtime
  se parte en ventanas de a lo sumo `vintages.max_vintage_dates_per_request`. Las filas vigentes
  que cruzan el borde de una ventana se repiten; `collapse_vintages` conserva solo la primera,
  que es su publicación real. Esto reemplaza el `realtime_floor` del ADR 0001, que fechaba toda
  la historia en la última vintage.
- **Series ligadas a `eval/`:** el principio 2 impide que `src/` las lea. Por eso `src/` expone
  `vintages --ids-file PATH` y será `eval/` (Fase 6) quien genere esa lista a partir de
  `known_events.yaml` y llame al comando. Hoy `known_events.yaml` está vacío.
- En una misma serie conviven filas `vintage_date IS NULL` (valor vigente) y filas con fecha
  (muestra). La clave `(source, series_id, date, vintage_date)` las separa. Los detectores leen
  las vigentes; el backtest, las fechadas.

### 3. Tablas de linaje con `run_id`

Toda decisión queda registrada con el `run_id` de la corrida que la tomó (`src/log.py`). Los
esquemas viven en `src/schemas.py` y pasan por `validate()`:

| Tabla | Clave | Contenido | Se llena en |
|---|---|---|---|
| `eligibility` | `run_id, source, series_id` | grupo, regla que la excluyó o clasificó, detalle (p. ej. `variante_de=<id>`), nivel geográfico, frecuencia, copyright | ahora (`src/eligibility.py`) |
| `detector_runs` | `run_id, detector` | n.º de pruebas, sobrevivientes BH y BY, q, parámetros | Fase 2 |
| `signals` | `signal_id` | se agregan `run_id` y `n_tests` (contra cuántas pruebas se corrigió) | Fase 2 |
| `series_term_links` | `run_id, source, series_id, text_source, term` | score de similitud en [0,1] y método | Fase 3/5 |
| `clusters` | `run_id, cluster_id, source, object_id` | rol (representante/miembro) y similitud | Fase 5 |

Ninguna serie se descarta sin motivo: el embudo escribe una fila por serie del catálogo, y
un test verifica que no falta ninguna.

### 4. Exportes para una web estática (sin construir la web)

`src/export.py` escribe en `data/exports/` (se sincroniza a R2 junto con lo demás):

- `funnel.json`: el embudo de la última corrida.
- `series/<NN>.json` y `series/index.json`: fichas de serie solo con metadatos, repartidas en
  buckets por id.
- Pendiente para la Fase 5: `crossings_graph.json` (grafo serie–término con score) y fichas con
  señales.

Las series con copyright de terceros no se exportan, solo se cuentan
(`copyrighted_withheld`).

### 5. API v2 de FRED: descarga en bloque por release

Verificado en la documentación oficial y en vivo el 2026-10-21:

- Endpoint: `fred/v2/release/observations`
  (https://fred.stlouisfed.org/docs/api/fred/v2/release_observations.html).
- Devuelve todas las series de un release con su historia completa: hasta 500.000
  observaciones por página y continuación con `next_cursor`.
- Autentica con el header `Authorization: Bearer <key>`, no en la URL.
- Trae `copyright_id`. No tiene parámetros realtime, así que solo da valores vigentes.
- Medido: el release 52 (Z.1) devolvió 500.000 observaciones de 2.606 series en 2,7 s y
  21,5 MB. Esas 2.606 series con v1 toman ~31 min (84 series/min): ~700× menos tiempo por
  serie.
- Todo FRED suma ~148 M de observaciones estimadas (fechas ÷ periodo): ~300 páginas más una
  por release (334 releases), es decir minutos en vez de días.
- Implementado en `fred.py download`: un release por tramo, cursor de progreso
  `v2_releases_done`. Solo se escriben las series objetivo; el resto del release se descarta.
- Un test verifica que v2 y v1 producen exactamente las mismas filas.

Riesgos:
- La documentación de v2 no indica un límite de solicitudes propio. Se asume el de v1
  (120/min) y v1 y v2 comparten un mismo `RateLimiter`.
- Una página trae series de todo el release, incluidas las no elegibles: más transferencia
  (~43 B/obs de JSON), no más solicitudes.
- La documentación advierte que, si FRED actualiza un release durante la descarga, puede
  mezclar series actualizadas y no actualizadas. `last_updated` permite detectarlo, y la
  actualización semanal vuelve a pedir lo que cambió.

## Consecuencias

- La descarga inicial deja de ser el cuello de botella. El costo dominante pasa a ser
  `crawl-meta` (~1 h de metadatos) y la muestra de vintages (v1, una serie por solicitud).
- Las observaciones bajadas en la verificación de la Fase 1 (en este contenedor) tienen
  `vintage_date = realtime_start` y solo la fila vigente. No se suben a R2: el VPS empieza de
  cero con el modelo de este ADR.
- `observations` (v1) queda para la actualización semanal (pocas series cambiadas) y para
  pruebas acotadas (`--category`, `--ids-file`).
