# ADR 0019 — Presupuesto duro de las fuentes (BLS, BEA, Census)

Fecha: 2026-11-11. Aprobada por David el mismo día, a raíz de la ingesta 42867229092 (job
130008713886).

## Qué pasó (del log del job)
- 18:36: FRED termina y sincroniza a R2. Empieza `infra/sources_update.sh` con presupuesto de 60 min.
- 18:39–18:42: el crawl de BLS procesa 10 de 66 unidades. 18:42–19:10: `download` lee, en orden
  alfabético, `ap, ce, cu, cw, cx, ei, ep, fm, jt, kv, le, ln, lu, nb` (`ln`: 6,9 M filas en ~4,5 min).
- **19:10:10 es la última línea de log.** A las 23:35 GitHub cancela el job por su límite (355 min).
  BEA y Census no corrieron; BLS no sincronizó nada, porque `sync_r2.sh` va DESPUÉS de cada fuente y
  nunca se llegó. Cuatro horas y cuarto de silencio.
- El presupuesto (60 min, vencía hacia las 19:37) no cortó nada: ya iba a media unidad.

## Causas (lo comprobado y lo que no)
Comprobado en el código:
1. **El presupuesto solo se miraba ENTRE unidades** (`download`: antes de empezar cada una).
   Dentro de una unidad (leer un archivo enorme, escribir) nada miraba el reloj.
2. **Ninguna capa superior**: `sources_update.sh` no tenía `timeout`, así que un proceso colgado
   seguía hasta el límite del job.
3. **Ningún log de inicio ni de progreso por unidad**: el único mensaje salía al terminar la lectura
   (`valores de encuesta leídos`), así que desde fuera no se podía distinguir «leyendo» de «colgado»
   ni saber qué unidad era. La unidad que sigue a `nb` no se puede identificar con el log disponible.
4. **Memoria sin tope en `_fetch_unit`**: guardaba en un conjunto TODAS las claves (serie, fecha) de la
   unidad para deduplicar. Medido: ~180 B por clave (`tracemalloc`, 1 M de claves), es decir ~1,2 GB para `ln`
   (6,9 M) y más de 5 GB para una unidad de ~30 M de filas, en un runner de ~7 GB de RAM.
   Con la memoria agotada el runner entra en swap y el proceso avanza a paso de caracol sin morir ni
   escribir logs: es la causa que mejor encaja con el síntoma, pero **no está demostrada** (el log no
   trae memoria ni la unidad que sigue a `nb`). Otras causas posibles: una lectura de red que gotea
   sin pasar el timeout de lectura de 300 s, o una escritura colgada. Por eso la solución no
   depende de cuál fue: acota las tres.

## Decisión
1. **Presupuesto duro dentro de la unidad** (`src/ingest/common.py`): `download` y `crawl` fijan un
   instante límite y lo comprueban cada `deadline_check_rows` filas (20.000) y antes de cada escritura.
   Al pasarse se lanza `BudgetExceeded`: la unidad queda sin estado (el estado por unidad se guarda solo
   al terminar), lo escrito son upserts completos, y la corrida siguiente la repite sin duplicar.
2. **Tope por unidad** `unit_max_min` (40): una unidad enorme no se come el presupuesto de las demás.
   Las unidades interrumpidas se anotan (`kind = values_interrupted`) y **pasan al final de la cola**
   en la corrida siguiente, para que no le quiten el turno a las demás cada semana.
3. **Memoria acotada**: la deduplicación es por lote (se vacía al escribir; `write_batch_rows` = 500.000
   filas). Una observación repetida en otro lote es un upsert idempotente con el mismo valor.
4. **Watchdog en el proceso** (`start_watchdog`): pasado presupuesto + `hard_grace_min` (10), registra
   el error y termina con código 11. Cubre lo que bloquea sin devolver el control a Python.
5. **`timeout` por fuente en `infra/sources_update.sh`**: presupuesto + `SOURCES_GRACE_MIN` (15), con
   `-k 60`. Una fuente cortada (124/137) o por su watchdog (11) se anota, **se sincroniza lo que dejó hecho**
   y se sigue con la siguiente. La sincronización tiene su propio `timeout` (`SOURCES_SYNC_TIMEOUT_MIN`, 30) y
   su fallo tampoco detiene las demás fuentes. `SOURCES_TOTAL_MIN` (240) reparte el tiempo del script:
   una fuente sin tiempo útil se salta y queda pendiente (no es fallo). Un corte por `timeout` o watchdog
   o un fallo de sincronización sí hacen terminar el script en error, para que se vea.
6. **Progreso que no se pierde**: el estado por unidad ya era reanudable; ahora además se sincroniza a R2
   después de cada fuente aunque se haya cortado, y un corte ya no depende del límite de 355 min del job.
7. **Logs** (JSON) de inicio y fin por unidad y por fase: `unidad iniciada`, `unidad en progreso` cada
   `progress_rows` filas (1.000.000), `unidad terminada` con `lectura_s`, `escritura_s`, `seconds` y `rss_mb`,
   `unidad interrumpida por el presupuesto`; y en el crawl `crawl: unidad iniciada/terminada`.
   La próxima vez que algo se cuelgue se verá la unidad, el tiempo y la memoria.

## Qué no cambia
La lógica de qué se descarga, el orden de unidades sanas (alfabético), el esquema ni los datos. Con presupuesto
suficiente el resultado es idéntico al anterior.

## Límites
- No hay forma de verificar desde aquí cuál de las causas posibles fue la real; el primer log de la próxima
  corrida (con `rss_mb` por unidad) lo dirá. Si una unidad sola no cabe en 40 min seguirá interrumpiéndose
  cada semana: se verá en `units_interrupted`, y la salida es leerla en tramos (no se hizo aquí).
- Un `SIGKILL` en mitad de una escritura deja un swap a medias que `store.recover` limpia en la
  escritura siguiente (ya existía). La sincronización posterior ignora los `.tmp`.
- El watchdog usa `os._exit`: no ejecuta limpieza; el registro de la corrida (`runs.jsonl`) puede quedar sin
  cerrar.
