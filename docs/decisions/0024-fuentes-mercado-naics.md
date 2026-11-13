# ADR 0024 — Fuentes de mercado por NAICS (CBP, BDS, Censo Económico)

Fecha: 2026-11-13. Aprobada por David (rediseño de oportunidades, Etapa 2, bloque 2).

## Decisión
Tres adaptadores nuevos sobre el contrato `SourceAdapter` de `src/ingest/common.py`, con el esquema
común y la misma prueba de esquema e idempotencia (`tests/test_sources_contract.py`):

| Fuente | Endpoint | Variables | `series_id` |
|---|---|---|---|
| `cbp` | `api.census.gov/data/{año}/cbp` | ESTAB, EMP, PAYANN | `cbp:ESTAB:6231` |
| `bds` | `api.census.gov/data/timeseries/bds` | FIRM, ESTAB, ESTABS_ENTRY_RATE, ESTABS_EXIT_RATE | `bds:ESTABS_ENTRY_RATE:6231` |
| `econcensus` | `…/{2017,2022}/ecnbasic` y `ecnsize` | FIRM, ESTAB, RCPTOT, EMP; `CONCENFI=*` (VAL_PCT); `EMPSZFF=*` (FIRM) | `econcensus:CR4:6231`, `econcensus:FIRM_SIZE:6231:510` |

- **Son métricas de mercado para las fichas, no entran a los detectores.** No están en
  `detect.sources` y `validate_config` rechaza cualquier fuente de `MARKET_ONLY_SOURCES` allí
  (test `test_market_sources_never_enter_the_detectors`). Son anuales y cortas.
- **Cero temas (principio 1).** Se pide todo el árbol NAICS con `NAICS*=*` (comodín); no hay un
  solo código escrito. Las variables de cada vintage se descubren en `data.json` y
  `variables.json`: el nombre de la variable NAICS cambia (NAICS1997/2002/2012/2017/2022). Las
  vintages de CBP sin variable NAICS (1986–1997, con SIC) se saltan y se cuentan en el log.
  `config.yaml` solo trae nombres de variables oficiales de Census y el umbral de «empresa
  pequeña» (20 empleados), no industrias.
- **Una unidad por fuente** (todas las vintages juntas): una serie NAICS cruza vintages y el
  contrato asigna cada serie a una unidad. La versión de la unidad es el hash de `modified` de
  los datasets y de las variables pedidas.
- **Embudo.** `sources.<fuente>.ignore_staleness: true` exime a estas fuentes de la regla
  «inactiva» (el último año publicado, p. ej. 2023, ya pasa los 730 días de `max_staleness_days`
  y el dato sigue siendo el vigente). CBP y BDS caen en `nacional`; el Censo Económico (solo 2017
  y 2022) en `nuevas`, que el ingestor descarga igual (ADR 0017). Los tests usan
  `ing.targets()` como objetivo.
- **Clase de medida** (ADR 0014), sin reglas nuevas: conteos → cantidad (unidad `Number`), RCPTOT y
  PAYANN → valor (`Thousands of dollars`), tasas y porcentajes → tasa (`Percent`). Los títulos
  no llevan el nombre de la industria («Number of establishments, NAICS 6231»): un título con
  «Real Estate» caería en la regla `\breal\b` de cantidad. El nombre sale de la tabla NAICS.
- **NAICS por serie** (`src/rank/naics.py`): el código va en el `series_id` (`ID_FORMATS`). Un
  código de 2017 que cambió se pasa con la concordancia oficial; uno de una vintage más antigua
  se ancla a su ancestro vigente en 2022 (prefijo o rango como `44-45`; método `ancestro_naics`).
  Cobertura medida sobre la descarga completa (sin el total `00`): CBP 99,9 %, BDS 100 %,
  Censo Económico 100 %. Quedan fuera los catch-all `95` y `99` de CBP.
- **Tabla derivada** `data/processed/market/naics_metrics/` (`python -m src.market build`), clave
  `(naics, metric, year)`: `firms`, `establishments`, `employment`, `payroll`, `revenue`, `cr4`,
  `cr8`, `cr20`, `cr50`, `small_firm_share`, `entry_rate`, `exit_rate` (más `*_bds` y `*_ec` donde
  otra fuente mide lo mismo con otra definición). `small_firm_share` = empresas en clases de
  tamaño con tope < 20 empleados ÷ empresas en todas las clases por empleo (el tope sale de la
  etiqueta oficial de la clase).

## Límites de cada API
- **CBP**: el NAICS de cada año es el de su vintage; los códigos no se armonizan entre revisiones
  (2002/2007/2012/2017/2022). Una serie `cbp:ESTAB:447` puede cambiar de significado. Valores
  suprimidos o negativos se omiten. Sin rezago menor de ~1,5 años.
- **BDS**: usa NAICS 2017 en todos los años. Solo nacional. Con `YEAR=*` devuelve todo en una consulta.
- **Censo Económico**: solo 2017 y 2022 (`min_vintage`; el esquema de 2012 es otro). Los
  porcentajes de concentración vienen por «N mayores» y se redondean a 0,1 %. Las clases de
  tamaño solo existen para empresas que operaron todo el año.
- **Wildcards**: `NAICS*=*` y `NAICS*=623*` funcionan (verificado el 2026-11-13); no se
  usan comas para varios NAICS. Muestras: `MARKET_NAICS_PREFIX=623`.
- **Cupo**: 60 solicitudes/min con key. Una corrida completa son ~45 solicitudes (CBP 26,
  BDS 1, Censo 6 + variables): CBP tarda ~70 s por el ritmo. La semanal tiene presupuesto
  propio de 20 min por fuente (`SOURCES_BUDGET_MARKET_MIN`) y corre `src.market build` al final.
- **Volumen** (descarga completa del 2026-11-13): CBP 7.614 series, BDS 1.576, Censo
  Económico 40.754 (la mayoría, clases de tamaño); 268.834 filas en `naics_metrics`, 2.710 NAICS.

## No incluido: SUSB
SUSB no tiene API. Los archivos de `www2.census.gov/programs-surveys/susb/` responden desde este
entorno (HTTP 200), pero un adaptador exige leer .xlsx (dependencia nueva) y descubrir el diseño de
columnas por año, y no pude comprobar el acceso desde Actions. La distribución por tamaño de
empresa ya la cubre `ecnsize` (2017 y 2022). Queda pendiente; no se inventó ningún dato.
