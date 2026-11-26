# ADR 0034 — Fuente EIA (electricidad) como origen independiente (2026-11-26)

Tarea aprobada por David en la revisión de la corrida `19979e2620e64e45beae7b9f420c39ab`.

## Acceso (verificado el 2026-11-26)
- Existen los **archivos masivos** `https://www.eia.gov/opendata/bulk/ELEC.zip` (302 MB; un JSON por
  línea; sin clave). Se usan en lugar de la API v2 (`api.eia.gov/v2`, que exige `api_key`: no hace
  falta `EIA_API_KEY`). Una unidad de descarga por archivo masivo (`sources.eia.bulk_datasets`; el
  alcance «electricidad» es el pedido, no una lista descubierta).
- El archivo trae 808 847 líneas: 708 833 son categorías o series de **planta** (llevan `lat`/`lon`;
  son puntos, no agregados) y se omiten (contadas en el log); quedan 100 014 series catalogadas:
  2 767 nacionales, 77 716 regionales (estado/región) y 19 531 sin código geográfico.
- Descarga única por versión (ETag): el zip se guarda en `data/raw/eia/`; catálogo y valores lo leen
  en streaming. Con `download.groups: [nacional]` solo se bajan los valores nacionales (2 719 series,
  331 995 filas; ~42 s en total). Los estados quedan catalogados y descargables cambiando el grupo.

## Diseño
- `src/ingest/eia.py` sobre `SourceAdapter`; periodos `YYYYMM`/`YYYYQn`/`YYYY` al primer día;
  sin ajuste estacional (`NSA`); `copyright` solo si EIA lo declara distinto de «None».
- `AGENCY = "U.S. Energy Information Administration"` en `src/rank/agencies.py`: origen propio.
- En `detect.sources`, en el contrato (`tests/test_sources_contract.py`, esquema e idempotencia), en
  `infra/sources_update.sh` y en `ingesta-fuentes.yml` (estado de R2 y corrida). `detect/dedup.py` no se modifica (otra sesión):
  `SOURCE_RANK.get(..., 1)` da a eia el mismo rango que FRED y el desempate por nombre prefiere eia.
- **NAICS:** los metadatos de EIA no traen NAICS. Con los métodos neutrales de `src/rank/naics.py`
  (código en metadatos/id, nombre oficial completo) **0 de 78 896** series regionales y nacionales
  con título reciben NAICS propio; no se escribió ninguna correspondencia a mano. Solo podrían
  heredar por cluster o por origen.
- **Origen común con FRED:** se buscaron valores idénticos (redondeo a 2 decimales, ≥10 meses desde
  2023, |valor| ≥ 20) entre las series M/Q de EIA y todo `series/fred` de R2: ninguna coincidencia
  real (las que salían con valores pequeños eran coincidencias de ceros/enteros), por lo que no se
  añade regla a `config/data_origin.yaml`. Límite: una republicación con otras unidades o redondeo no
  se vería con esta prueba; el detector de duplicados de `rank` la verá si entra a una evidencia.

## Primera prueba con datos reales (copia local, nada subido a R2)
Ver el PR. Resumen: el fenómeno `p4244657ff803` (empleo en generación eléctrica, NAICS 22111, solo
BLS) **no queda triangulado** con EIA.
