# ADR 0010 — Independencia por origen del dato

Fecha: 2026-11-04. Aprobado por David (tabla `data_origin` incluida).

## Problema

El principio 4 pide dos fuentes **independientes**. El ADR 0009 las contaba por agencia que
publica; pero una agencia puede republicar o derivar el dato de otra (Z.1 de la Fed arma los
bancos con los Call Reports que también alimentan al FDIC; la OCDE republica salarios de BLS).
Esos pares aparecían como dos fuentes y pasaban la triangulación sin serlo.

## Decisión

La identidad de fuente para triangular es el **origen** del dato:

1. `config/data_origin.yaml` (versionado, esquema validado en `src/rank/origin.py`): reglas
   `match` (source / release_id / agency / series_id_regex / units_regex) → `origin` + `chain`
   (raíz primero). Gana la primera que casa. Sin regla, el origen es la agencia (como antes).
2. Detector automático de duplicados (en `rank run`, sobre las series con señal): dos series
   con identidades distintas, ρ de crecimientos > `rank.origin.duplicate_rho` (0,98) y nivel
   casi igual (mediana de |a−b|/media ≤ `level_tol`, misma grilla, ≥ `min_overlap` periodos)
   se marcan "posible mismo origen". Sus identidades se unen: cuentan como **una** fuente.
   Es una marca para revisión humana, no un veredicto.
3. Tabla `processed/rank/rank_origin` por (run, serie): origen, cadena, regla, posibles copias.
   El reporte muestra "Origen del dato" (p. ej. "BLS → republicado por OCDE MEI") y el aviso.

Excepción al principio 1: la tabla contiene ids de release y regex de ids de serie, pero son
reglas de independencia metodológica, no temas. Vive en su propio archivo (`config/`), que
`config.yaml` no puede reemplazar: un test comprueba que la misma sección dentro de `config.yaml`
sigue fallando el guard, y otro que el esquema rechaza claves fuera de la lista permitida.

## Casos verificados contra la API de FRED (2026-11-04)

| Regla | Evidencia |
|---|---|
| release 482 (FDIC Quarterly Banking Profile) → Call Reports | nombre del release en `fred/releases` |
| release 52 (Z.1), ids `BOGZ1xx70/73/74/76…` → Call Reports | `BOGZ1FL763065105Q` = "U.S.-Chartered Depository Institutions; One-to-Four-Family Residential Mortgages…" |
| release 52, ids `BOGZ1xx075…` y título "Chain Weights" → BEA (índices de precios) | `BOGZ1FL075013203Q` = "Interest Rates and Price Indexes; Expenditures on Equipment; Chain Weights, Level" (unidades "Percent": por eso no se usa la regla por unidades). Tasas (`…072…`, `…073…`) no |
| release 205 (OECD MEI), ids `LC*`, `LR*`, `LF*` → BLS | `LCEAMN01USM189S` = "Labor Compensation: Earnings: Manufacturing: Hourly for United States", release 205 |

## Otros casos plausibles, NO incluidos (revisar con datos)

- Series de la OCDE MEI de EE. UU. de otros prefijos (producción, precios): vienen de BEA, Fed
  o BLS según el indicador; sin mapeo seguro por id, los cubre el detector de duplicados.
- Charge-Off and Delinquency (release 231): también deriva de reportes bancarios (FR 2644 /
  Call Reports); ver si coincide con QBP antes de añadir regla. (H.8 sí se añadió, abajo.)
- BEA ↔ Census (comercio exterior), BLS ↔ BEA (empleo en cuentas nacionales): el insumo es el
  mismo, pero la serie es distinta; no se unen salvo que el detector los marque.

## Límites

La tabla es incompleta por naturaleza; el detector solo atrapa copias casi exactas (mismo nivel).
Una derivación con otra transformación (índice, escala) escapa a ambos y queda para revisión.

## Corrección (datos reales, corrida 702b8ff3)

La primera versión no casaba el lado Z.1: HELOC `BOGZ1FL703065125Q` es sector 70 ("Private Depository Institutions") y los índices de precios de la Z.1 tienen unidades "Percent". Se añadió el sector 70 y la regla de índices pasó a id `…075…` + título `Chain Weights` (nuevo campo `title_regex`).


## Revisión 702b8ff3 (2026-11-04)

- **Z.1 ← BEA:** la regla `z1_price_indices_bea` ahora casa también títulos "price index" y
  "price deflator" (índices de precios de equipo y similares del sector 07, instrumento 075),
  además de "chain weights". Las series de BEA (`source: bea`) ya tienen origen BEA por su agencia.
- **H.8 = benchmark a Call Reports (no independiente):** la H.8 (FRED release 22) se estima con una
  muestra semanal y se ajusta a los Call Reports trimestrales; se trata como origen
  `call_reports` (regla `h8_call_reports`). Un candidato con H.8 más FDIC QBP o Z.1 de bancos cuenta
  como una sola fuente.
- **HELOC total de todos los sectores en la Z.1:** NO verificado en esta revisión (sin acceso a la
  documentación de la Z.1 desde el entorno). Hipótesis a confirmar: el sector de bancos viene de
  Call Reports (cubierto por la regla), y el total suma sectores con otras fuentes (uniones de
  crédito, hogares). Hasta confirmarlo, el total de todos los sectores conserva la agencia
  (Fed) y no se une a Call Reports.
- **Propuesta (sin implementar):** NY Fed Household Debt and Credit (viene del Equifax/CCP) y NCUA
  Call Report 5300 (uniones de crédito) se agregarían como adaptadores `SourceAdapter` en
  `src/ingest/` con su propia agencia y origen (`equifax_ccp`, `ncua_5300`), más reglas en este
  archivo para las series de la Z.1 que derivan de ellos. Requiere aprobación antes de ingestar.
