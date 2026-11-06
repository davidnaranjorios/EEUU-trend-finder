# ADR 0012 — Anclaje a industria NAICS

Estado: aceptado (rediseño, sesión 1). La unidad de análisis pasa de «cluster de series» a industria.

## Decisión
- Tablas oficiales de Census versionadas en `config/naics/` (fuente y fecha en `SOURCES.md`): nombres
  NAICS 2022 y concordancia SIC 1987 → NAICS 2022 (cadena oficial 2002→2007→2012→2017→2022, pesos 1/n;
  `scripts/build_naics_tables.py`). Son datos, no listas de temas: no están en `src/` ni en
  `config.yaml` (solo sus rutas, `rank.naics`), así que el guard del principio 1 no necesita excepción.
- `src/rank/naics.py`, tres métodos, siempre marcados en `naics_links.metodo`:
  `codigo_en_metadatos` (código «NAICS nnnn» explícito en título/ruta, validado contra la tabla),
  `heredado_cluster` (series sin código heredan de su cluster) y `sic_concordancia` (términos: SIC de las
  empresas → NAICS, ponderado por documentos).
- EDGAR guarda `term_freq.sic_top_json` (SIC de 4 dígitos, top `text.top_sic` por término): esquema de
  texto 5; `edgar.reprocess_below_schema: 5` fuerza el reproceso. Hasta entonces los términos quedan
  sin NAICS («pendiente de datos»); no se inventan filas.
- `rank run` escribe `naics_links` (run_id, source, object_id, naics, naics_name, peso, metodo) y la
  cobertura por fuente en `rank_runs.params_json.summary.naics_coverage`; el reporte muestra el NAICS
  de cada candidato y una tabla de cobertura.

## Límites
- BLS/BEA/Census no guardan hoy un campo de industria dedicado: el anclaje depende de que el código
  aparezca como «NAICS …» en título/ruta. La cobertura real (objetivo ≥ 80 %) se mide en la corrida real;
  si queda corta, el siguiente paso es leer el código de industria de los ids propios de cada adaptador.
- La concordancia SIC→NAICS es muchos-a-muchos; el peso uniforme es una aproximación declarada.

## Addendum S1b — diagnóstico de la corrida real (0 % de cobertura en todas las fuentes)
- `rank run` pasaba los objetos correctos y los formatos de código sí casan con la tabla oficial (2–6
  dígitos y rangos «31-33» incluidos): el fallo era de diseño. S1 solo buscaba el literal «NAICS nnnn»
  en título/ruta y ninguna fuente lo escribe: BLS titula con el nombre de la industria, BEA y FRED con
  texto libre, y los códigos de industria viven en tablas/ids propios.
- Corrección: BLS copia `naics_code` de la tabla de industrias de la encuesta (p. ej. `ce.industry`,
  que el adaptador ya descarga) a `category_path` como «NAICS x» (`codigo_en_metadatos`); BEA, FRED y
  Census (cuando el título no lleva código) casan un segmento del título con un nombre oficial NAICS
  2022 exacto (`nombre_oficial`, el código más amplio si el nombre se repite); FRED hereda de su copia
  en otra agencia (`heredado_origen`, duplicados de ADR 0010) y luego del cluster. Federal Register
  queda fuera del denominador (`rank.naics.not_applicable_sources`).
- No se versionó una tabla `ce.industry`: download.bls.gov devuelve 403 al entorno de desarrollo; el
  adaptador la lee en la ingesta (donde sí tiene User-Agent válido). Se aplica a cualquier encuesta BLS
  cuya tabla de industrias traiga `naics_code`. Requiere re-ingestar los metadatos de BLS.
- BEA: no se encontró un campo NAICS en la API; se usa el nombre oficial. Una concordancia publicada
  por BEA queda como mejora si la cobertura de BEA es baja.
- Pages: los archivos del explorador ya son ≤ `report.explorer.max_file_kb` (2 MB); el timeout de
  `syncing_files` (138 archivos, 66 MB) es por volumen total, no por archivos grandes. Alternativa
  documentada: `PUBLISH_TARGET=cloudflare_pages` (`infra/publish_site.sh`, ADR 0009).
