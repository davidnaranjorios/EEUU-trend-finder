# ADR 0027 — Ingesta numérica en tres workflows y cadena semanal

Fecha: 2026-11-17 (aprobado por David).

## Problema
El workflow «Ingesta» del 2026-11-15 (run 43721407427) fue cancelado a las ~5 h 55: FRED update
2 h 40, vintages 18 min, BEA 1 h; BLS se cortó y Census y las fuentes de mercado (`cbp`, `bds`,
`econcensus`, ADR 0024) nunca corrieron. Además, `processed/text_structure/` (ADR 0026) no estaba en
las rutas de texto que se suben a R2 y se perdió tras el reprocesamiento completo.

## Decisión
1. Tres workflows, mismo grupo de concurrencia `ingesta` (nunca dos sync a R2 a la vez), cada uno con
   `timeout-minutes` ≤ 330 y su propia sincronización:
   - `ingesta.yml`: FRED (update + eligibility + observations + vintages + export). Única con `cron`.
   - `ingesta-fuentes.yml`: `sources_update.sh bls bea census`, `SOURCES_TOTAL_MIN=300`. El presupuesto
     por fuente (ADR 0019) corta limpio y lo pendiente sigue en la próxima corrida.
   - `ingesta-mercado.yml`: `sources_update.sh cbp bds econcensus` y `src.market build`.
2. Cada workflow trae de R2 solo sus rutas y sincroniza solo esas (excepto FRED, que trae y sube todo
   como antes, porque `weekly_update.sh` sincroniza todo `data/`).
3. Cadena semanal: FRED → fuentes → mercado → texto → detectores → reporte. Cada eslabón lanza el
   siguiente con `gh workflow run` (`actions: write`), solo con `success()` y solo si el eslabón
   recibió `encadenar=true` (el cron de FRED lo activa; una corrida manual no encadena por defecto).
   El texto solo lanza detectores cuando no quedan periodos pendientes. Si algo falla, la cadena se
   detiene (freno). El `cron` propio del texto se retiró: ahora lo dispara la cadena.
4. `text_structure` se agrega a `TEXT_PATHS` (`run_texto.sh`) y al pull de `ingesta-texto.yml`; un test
   compara contra las tablas de texto declaradas en `src/catalog.py`.
5. Un periodo con documentos y sin partición en `text_structure` se reprocesa aunque su esquema sea el
   actual (`TextState.needs_run(structure_missing=...)`), con la reanudación y el freno de ADR 0026.
   El reprocesamiento lo lanza David tras el merge.
