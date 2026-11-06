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
