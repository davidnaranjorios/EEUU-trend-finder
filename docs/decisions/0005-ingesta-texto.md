# 0005 — Ingesta de texto: EDGAR (10-K) y Federal Register

- Fecha: 2026-11-03
- Estado: aceptada (plan aprobado por el responsable del proyecto el 2026-11-03)

## Contexto

La Fase 3 necesita señales de texto independientes de las series numéricas: el texto de los
10-K (referencia de diseño: Hanley & Hoberg 2019, que siguen riesgos emergentes en el texto de
los reportes anuales) y la regulación nueva del Federal Register. Restricciones:

- correr en GitHub Actions (6 h por job, máquina efímera) con estado en R2 (ADR 0004);
- descarga incremental, idempotente y reanudable por tramos;
- **no guardar el texto completo** (decisión del 2026-11-03): solo `text_hash` y la tabla de
  frecuencias por periodo;
- cero temas escritos a mano (principio 1).

## Límites verificados (2026-11-03)

| Fuente | Dato | Cómo se verificó |
|---|---|---|
| SEC EDGAR | "Current max request rate: 10 requests/second"; User-Agent con nombre y correo | sec.gov, "Accessing EDGAR Data" |
| SEC EDGAR | `full-index/<año>/QTR<n>/form.gz`: 5,7 MB (2024Q1) | descarga en vivo |
| SEC EDGAR | `daily-index/bulkdata/submissions.zip`: 1,57 GB, actualizado a diario, con `primaryDocument` | HEAD en vivo |
| SEC EDGAR | 10-K + 10-K405: 8.852 en 2006, 7.985 en 2015; documento principal reciente ~2,2 MB | conteo sobre form.idx |
| Federal Register (API) | `count` topado en 10.000 por consulta; la página de documentación redirige a una página anti-bots y no se pudo leer | llamadas en vivo |
| govinfo (GPO) | `bulkdata/FR`: XML diario desde 2000 (no hay 1999), zip mensual (~12 MB) y anual (188 MB en 2024) | listado JSON en vivo |

govinfo no publica un límite de solicitudes; se usan 2 req/s y reintentos con backoff.

## Decisiones

### 1. Fuentes y unidades

- **EDGAR:** 10-K y 10-K405 desde 2006 (Item 1A obligatorio para ejercicios cerrados desde el
  1-dic-2005). 10-K/A, 10-KT, 10-KSB y demás variantes quedan en el linaje como `excluido`
  con el formulario en `detail`. Un documento por accession (los co-registrantes no se cuentan
  dos veces). Secciones `item_1a` e `item_7`.
- **Federal Register:** XML masivo de govinfo, no la API (≈330 descargas en total en vez de
  cientos de miles de textos). Un documento por elemento RULE, PRORULE, NOTICE o PRESDOCU;
  `section` = tipo en minúsculas, `doc_id` = número "FR Doc.", `entity` = agencia.
- **Periodo:** trimestre de presentación (EDGAR, `2024Q1`) y mes de publicación (FR,
  `2024-01`). Es la granularidad más fina que se guarda; el detector puede agregarla.

### 2. Extracción de secciones (EDGAR), solo por estructura

- HTML/iXBRL → texto con lxml: se descartan `script`, `style`, `ix:header` y lo oculto con
  `display:none`; los espacios y saltos de línea del fuente se colapsan y cada bloque (p,
  div, tr…) termina en salto de línea.
- Encabezados al inicio de línea: `Item 1A` hasta `Item 1B`/`Item 2`; `Item 7` (no `7A`) hasta
  `Item 7A`/`Item 8`. Entre cada inicio y el primer fin que le sigue se toma **el tramo más
  largo**, así el índice del documento (tramos de una línea) nunca gana a la sección real.
- Sección más corta que `edgar.min_chars` → `muy_corta` (típico: "no aplica" de las empresas
  pequeñas, o Item 7 incorporado por referencia a otro anexo). Sin encabezados →
  `sin_seccion` (típico: fideicomisos y emisores de ABS, que no tienen esos Items).
- Si `primaryDocument` no aparece en submissions, se usa la presentación completa (.txt) y su
  primer documento del tipo del formulario.

### 3. N-gramas y frecuencias sin guardar el texto

- Normalización NFKC + minúsculas; tokens `[a-z]+`; n-gramas de 1 a 3 que no cruzan
  puntuación de frase o cláusula; un documento cuenta una vez por n-grama.
- Cada n-grama se representa por un hash estable de 64 bits (blake2b). Al cerrar el periodo se
  cuentan los documentos por hash y se conservan los que llegan a `text.min_doc_count`; solo
  para esos se recupera el texto del n-grama releyendo una copia temporal local de las
  secciones normalizadas, que se borra al terminar el tramo y nunca se sube a R2.
- **Sin lista de stopwords** (principio 1) y sin filtro por proporción en la ingesta: un umbral
  de proporción calculado periodo a periodo haría aparecer y desaparecer n-gramas entre
  periodos y crearía saltos artificiales. El filtrado de palabras funcionales se hace en el
  detector (parte B), con reglas sobre todos los periodos a la vez.
- Los conteos por debajo de `min_doc_count` quedan censurados (no aparecen); el detector debe
  tratarlos como "menos de `min_doc_count`", no como cero exacto.
- Duplicados: dentro de un periodo y sección, un `text_hash` repetido cuenta una sola vez
  (`duplicado`, con `duplicado_de=<doc_id>`).

### 4. Esquemas

- `term_freq`: se agrega `section` a la clave → `(source, section, period, term)`. Item 1A,
  Item 7 y los tipos del Federal Register quedan separados sin convertirse en "fuentes"
  distintas: para la triangulación la fuente es `source` (`edgar`, `federal_register`).
- `text_docs`: sin cambios; una fila por documento y sección `ok`.
- Linaje nuevo `lineage/text_ingest`, clave `(run_id, source, doc_id, section)`, con
  `status ∈ {ok, sin_seccion, muy_corta, duplicado, excluido}`, `detail` y `n_chars`.
  `section = "*"` marca decisiones sobre el documento entero. Ningún documento se descarta
  sin motivo.

### 5. Almacenamiento, tramos e idempotencia

- Layout por periodo, no por bucket de hash: `<tabla>/<fuente>/<periodo>/part-*.parquet`.
  El tramo es el periodo y reprocesarlo **reemplaza la partición completa**
  (`store.replace_partition`, mismo swap atómico que `upsert`), así que repetirlo no duplica
  filas ni archivos.
- Estado en `ingest_state/text/<fuente>.json`: un periodo cerrado (terminado hace más de
  `closed_grace_days`) no se vuelve a pedir; uno abierto se reprocesa como mucho una vez cada
  `text.open_refresh_hours`, para que el bucle de tramos termine.
- Una corrida cortada a mitad de periodo no escribe nada de ese periodo; la siguiente lo repite
  completo (test).
- `infra/run_texto.sh`: tramos de 12 meses (FR) o 2 trimestres (EDGAR) y, tras cada uno,
  `infra/sync_r2.sh` **solo con las rutas de texto**, para no tocar las tablas de FRED.
  `pull_r2.sh` y `sync_r2.sh` aceptan ahora rutas opcionales; sin argumentos se comportan
  igual que antes.
- `.github/workflows/ingesta-texto.yml`: semanal y manual, mismo grupo de concurrencia
  (`ingesta`) que FRED; secret `SEC_USER_AGENT`.
- Vistas DuckDB locales `text_docs`, `term_freq` y `text_ingest` en `src/catalog.py`. En R2 no
  se crean todavía (un glob vacío hace fallar la vista); se consultan sobre la copia local.

## Volumen y tiempo

Medido en este contenedor el 2026-11-03; ver la sección "Medición" para las cifras.

## Riesgos

- La extracción por encabezados falla en documentos con formato atípico; queda medida en el
  linaje (`sin_seccion`, `muy_corta`) por periodo.
- `submissions.zip` (1,57 GB) se baja una vez por corrida de Actions cuando un trimestre tiene
  más de `edgar.submissions_api_max_ciks` CIK; si no, se consulta la API por CIK.
- Si la SEC responde 403 o 429 sostenido, la corrida se detiene (no sigue a ciegas) y el
  periodo queda pendiente.
