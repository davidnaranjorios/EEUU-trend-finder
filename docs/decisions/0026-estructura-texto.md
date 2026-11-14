# ADR 0026 — Inventario de la estructura del texto (Etapa 1) y reanudación del reprocesamiento

Fecha: 2026-11-13. Aprobado por David el 2026-11-13 (`docs/plan-estructura-texto.md`, Etapa 1).
**Solo describe:** no cambia ningún detector ni `term_freq`. (El 0025 es el de la rama
`claude/ajustes-seleccion`; el 0024, el de `claude/bloque2-mercado`.)

## Decisiones
1. **Tabla nueva y aditiva `processed/text_structure/<fuente>/<periodo>/`**, una fila por documento
   (clave `(source, doc_id)`), validada con `validate()` y escrita por periodo como `text_docs`
   (idempotente: reprocesar un periodo reemplaza su partición). Campos que la fuente no trae: NULL.
   - EDGAR: formulario, SIC de 4 dígitos, estado de incorporación y de la sede, cierre fiscal,
     categoría de emisor (de `submissions`, **en su estado actual**, no el histórico), todos los
     Items que aparecen como encabezado con su largo (`items_json`, aunque solo se analicen 1A y
     7), tipo de archivo (HTML o texto) y, para unos pocos 10-K por trimestre
     (`text.structure.facts_per_period`), el número de conceptos XBRL de `companyfacts` con algún
     hecho en esa presentación (`accn`): solo cobertura, ningún valor se ingiere.
   - Federal Register: tipo (etiqueta del XML), agencia y subagencia, referencias al CFR (elemento
     `CFR`), temas oficiales de indexación (`LSTSUB`, «List of Subjects») y huella de formato (las
     etiquetas hijas de `PREAMB`). La marca de «significativa» no existe en el XML de govinfo:
     queda NULL (columna reservada).
2. **`structure-report`** (`uv run python -m src.ingest.text structure-report [--processed-dir D]`)
   escribe `docs/estructura-texto.md`: cobertura por campo y año, Items por año y cuándo nacen,
   empresas por industria y año (SIC de 4 y 2 dígitos) y cambios de formato. Los umbrales de lectura
   están en `text.structure` de `config.yaml`; ninguna lista de industrias o temas.
3. **Reanudación.** `SCHEMA_VERSION` pasa a 6 y `reprocess_below_schema` a 6 en ambas fuentes: los
   periodos cerrados con un esquema menor se reprocesan una vez; los ya procesados con el 6 no se
   repiten (estado por periodo en `ingest_state/text/`). Cada corrida tiene un presupuesto
   (`text.run_budget_minutes` = 330 min): antes de cada periodo se comprueba que quepa (margen
   `period_margin` sobre el periodo más lento de la corrida; estimación inicial por fuente), y si no,
   sale con código 12 tras sincronizar. `infra/run_texto.sh` deja `data/processed/ingest_state/.pending`
   y el workflow se vuelve a lanzar solo (`chain`, `actions: write`) hasta que no queden pendientes.

## Límites
- Los metadatos de EDGAR salen de `submissions` hoy: la industria o el estado de una empresa pueden
  haber cambiado desde la presentación. Obtener el encabezado de cada presentación cuesta una
  solicitud más por documento; se decide en la Etapa 2 con el inventario a la vista.
- El inventario de Items usa encabezados «Item N» al inicio de línea: es una descripción, no un
  analizador completo (los 10-K muy antiguos o con formato libre pueden no listar todo).
- Estimación del reprocesamiento completo: ver el PR (corridas y cómo lanzarlas).

**Freno del encadenado (2026-11-14, pedido por David):** `run_texto.sh` crea
`ingest_state/.progress` cuando alguna invocación procesa al menos un periodo (`--progress-file`).
Si al terminar quedan periodos pendientes y no hubo avance, sale con código 13: el paso de
encadenar solo corre con `success()`, así que no se lanza otra corrida idéntica sin fin.
