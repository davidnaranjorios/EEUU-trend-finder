# ADR 0032 — Capa 3, pregunta 2: «¿qué consecuencias genera?» e insumo-producto de BEA

Fecha: 2026-11-24 (aprobado por David; Sesión 3 de `docs/plan-segunda-etapa.md`).

## Fuente nueva: `bea_io`
- Adaptador `src/ingest/bea_io.py` sobre `SourceAdapter` (subclase de `BeaAdapter`: mismo cliente,
  límites y análisis de `GetData`). Dataset `InputOutput`; `GetParameterValues(TableID)` ofrece solo
  tablas **Summary** y **Sector** (verificado el 2026-11-24: 56–61 requerimientos totales, 258/259
  uso, 261/262 oferta). No hay nivel detalle por la API. Se pide la tabla de uso **Summary
  (TableID 259)**, años 1997–2025, con `table_regex` sobre la descripción oficial; el Sector (258)
  agrega las mismas cifras y se omite. Una sola solicitud `Year=ALL` (~31 MB, ~110 s, 134 615 celdas).
- `series_id = InputOutput:259:<fila>.<tipo>.<columna>.<tipo>:A`, fecha = 1 de enero, millones de
  dólares corrientes. El título es «fila → columna» (campo `title_fields` de `parse_data`, nuevo y
  opcional; `bea` no cambia).
- Fuera de `detect.sources` (`MARKET_ONLY_SOURCES`, ADR 0024). Nota: `bea` (genérica) también
  recorre `InputOutput`; esas series siguen alimentando los detectores como antes.
- Workflow `ingesta-mercado.yml` (+ `BEA_API_KEY`) y `sources_update.sh`; `reporte.yml` trae además
  `processed/text_structure`.

## `market/io_flows` (`uv run python -m src.market build`)
Clave `(year, from_industry, to_industry)`: código BEA y NAICS 2022 de cada lado (concordancia
oficial `bea_industry_naics.csv`; sin código, NAICS nulo = `sin_dato`), descripciones, valor,
`from_kind`/`to_kind` y `demand_category`. Clasificación estructural, sin lista de industrias:
- columna **industria** si su código también es fila de la tabla; **total** si su descripción
  empieza con «Total»; el resto es **demanda final**, con categoría por expresión sobre la
  descripción oficial (`market.io.demand_categories`: hogares, gobierno, inversión, exportaciones;
  gobierno antes que inversión);
- fila **industria** si es columna industria; **total**; **otro** (valor agregado, impuestos).

## Tabla `processed/rank/phenomenon_consequences`
Clave `(run_id, phenomenon_id, consequence, metric)`; valor, texto, unidad, periodo, fuente, nivel
✅ directo / 🟡 indirecto / ⚪ sin dato, `public` (copyright), nota y posición. `uv run python -m
src.rank consequences [--run-id ID]`; `rank run` lo corre después de «qué cambió». Sin IA.
- **Compradores**: por categoría de demanda final y las 5 industrias compradoras principales, como %
  del uso total del producto (el mayor total de la fila). El código BEA sale del NAICS del fenómeno
  por la concordancia (el más específico; 🟡 si el código BEA es un ancestro).
- **Proveedores**: las 5 industrias principales, % de los insumos de otras industrias.
- **Escasez**: producción real por trabajador (fila de productividad de «qué cambió»); nómina por
  trabajador y frente a la economía (CBP: `payroll`/`employment`; `PAYANN` viene en miles de dólares
  pero la serie lo etiqueta «Number», de ahí `rank.consequences.payroll_multiplier`); vacantes: serie
  ligada al NAICS por `naics_links` cuyo título coincide con `vacancies_regex`.
- **Pérdida de participación**: tasa de salida (BDS) en veces la de la economía y cambio de CR4.
- **Márgenes**: ingresos − gastos si hay series de ambos tipos del mismo NAICS y mismo alcance
  (título igual tras «for»), p. ej. QSS.
- **Problemas declarados**: sobrerrepresentación = (docs del NAICS con el término ÷ docs del NAICS) ÷
  (docs con el término ÷ docs totales) en la ventana de la señal, con `term_freq.sic_top_json`,
  `text_structure` (SIC por documento) y la concordancia oficial SIC→NAICS. Umbral
  `min_overrepresentation` (2×), `min_companies` (5), `min_docs_naics` (3). Sin ninguno: ⚪ «sin
  riesgos propios de la industria»; con menos de 5 empresas se sube de nivel NAICS.

## Reporte
Sección «¿Qué consecuencias genera?» entre «¿Qué cambió realmente?» y «Antecedentes»; en la portada,
una línea con el comprador principal y la consecuencia de mayor magnitud (la cifra «veces el
promedio» que más se aleja de 1). Las filas con `public = false` no salen; la barrera no cambia.

## Límites
- `sic_top_json` guarda solo los 5 SIC más frecuentes por término: el conteo del NAICS es un piso.
- `text_structure` cubre una muestra de los 10-K (≈24 mil documentos): para industrias con pocas
  empresas en la ventana (p. ej. residencias de cuidado) el resultado es ⚪.
- BEA registra los pagos de Medicare y Medicaid como consumo de los hogares: la demanda «gobierno»
  de servicios de salud aparece casi en cero y «hogares» domina.
