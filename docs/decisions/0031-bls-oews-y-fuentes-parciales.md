# ADR 0031 — OEWS (`oe`) en BLS y fuentes cortadas = parciales

Fecha: 2026-11-23. Aprobado por David. Amplía ADR 0007, 0019 y 0027.

## Contexto

Corrida «Ingesta fuentes» 45056503024 (2026-11-22): BLS entró en `oe` (OEWS, 1.948.905 series
objetivo) a las 18:49 y a las 19:52 el watchdog la mató (`budget_s 3600`, `grace_s 600`,
`rss_mb 2307.9`). `sources_update.sh` salió con `fallaron bls(watchdog)` (exit 1) y la cadena
(ADR 0027) no lanzó mercado, texto, detectores ni reporte.

## Causa

`BlsAdapter._catalog_meta` pasaba los ids de la unidad como parámetro de DuckDB
(`list_contains(?, series_id)`): cada fila del catálogo se compara con la lista entera, O(n²). Medido
con el archivo real `oe.data.0.Current` (6,0 M de filas, 331 MB):

| series pedidas | antes | después |
|---|---|---|
| 100.000 | 26 s | — |
| 200.000 | 81 s | — |
| 1.900.000 | > 1 h (extrapolado, cuadrático; coincide con el log) | 16 s, RSS máx. 1,25 GB |

Ocurría ANTES de leer la primera fila: no emitía «unidad en progreso» ni disparaba
`unit_max_min` (que solo se mira dentro del bucle de filas), por eso solo lo paró el watchdog.

## Decisiones

1. `_catalog_meta(unit, ids)` filtra por la columna `unit` en DuckDB (sin listas), lee solo las
   columnas usadas (`read_table(columns=...)`) y filtra por pertenencia en Python. Los atributos
   repetidos se comparten y el «falta ver primera/última observación» pasa de un conjunto de fechas a
   un entero por serie. La lectura ya filtraba en streaming: no se cargan las 6 M de filas.
2. «Descarga solo si cambió» ya existía (`values_version` = huella de nombre, tamaño y fecha de los
   `.data.*` + mismo número de series). `oe` nunca se marcó «sin cambios» porque el estado se escribe
   solo al terminar la unidad con éxito, y no había terminado nunca. Con el arreglo, la segunda
   corrida la salta (test `test_unchanged_unit_is_not_reprocessed`).
3. No hace falta partir `oe` por tramos: con 16 s cabe de sobra. Si alguna encuesta futura no cabe, la
   unidad interrumpida ya pasa al final de la cola (ADR 0019); la lectura por tramos sigue como mejora.
4. **Cortada = parcial.** En `infra/sources_update.sh`, una fuente cortada por `timeout` (124/137) o
   por su watchdog (11) se sincroniza, escribe un aviso en el log y en `$GITHUB_STEP_SUMMARY` y NO
   cuenta como fallo: el script sale con 0 y la cadena sigue. Un error real (excepción, credenciales,
   esquema; cualquier otro rc, o fallo de sincronización) sigue en `failed` y sale con 1.

## Verificación

Ver el PR: tests `test_catalog_meta_scales_linearly_and_only_returns_the_unit`,
`test_unchanged_unit_is_not_reprocessed` y los de `sources_update.sh` en
`tests/test_presupuesto_fuentes.py`. Sin hardcodeo: nada de series, encuestas ni temas nuevos.
