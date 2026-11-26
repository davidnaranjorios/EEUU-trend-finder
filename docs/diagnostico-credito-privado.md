# Diagnóstico: crédito privado (`BOGZ1FL46…`) y trayectoria `no_confirmada` (2026-11-26)

Solo lectura de R2 (`src.catalog.connect_r2`); no se tocó código ni se subió nada.

## Qué son las series
`BOGZ1FL46…` (Z.1, FRED release 52) es el sector 46 «Interval funds and tender offer funds», **no**
compañías de desarrollo de negocios (BDC). 42 series trimestrales (`…Q`) y 42 anuales (`…A`).
Las 42 trimestrales tienen la misma forma.

## Fecha de inicio
- `series_meta.start_date`: **1945-10-01** (la serie nominal empieza en 1945Q4); `end_date` 2026-04-01.
- Primera observación en R2: 1945-10-01, 323 trimestres (18 nulos).
- **Primer valor positivo: 2021-10-01** (41 de 42; `…62083Q` nunca es positivo). Antes son 286 ceros.
  Es decir, la historia real es de **19 trimestres** (2021Q4–2026Q2). Ejemplo
  `BOGZ1FL463067205Q` (crédito privado): 15 811 (2021Q4) → 79 937 (2026Q2), CAGR ≈ 43 %.

## Por qué sale `no_confirmada` (`src/rank/trajectory.py`, sin modificar)
Dos causas encadenadas, ambas por serie joven:
1. **Ceros previos.** `classify` devuelve `(None, {})` si algún nivel es ≤ 0 (línea
   `(levels <= 0).any()`, porque trabaja en log). La serie completa con 286 ceros no se evalúa.
2. **Historia insuficiente aunque se recortaran los ceros.** Con los 19 trimestres, sin recesión en
   los últimos 10 años, el ancla queda a `recent_years`=3 años (12 trimestres) del final; la
   tendencia previa pide `prior_years`=5 años (20 trimestres) antes del ancla: índice 7−20 < 0, así
   que `_cagr` da `None` y no hay etiqueta (probado: `cagr_previo=None`, `cagr_reciente=0.431`).
Resultado: ninguna serie del cluster tiene etiqueta y el fenómeno queda `no_confirmada`; es la marca
descriptiva prevista, no un error de código.

## q ≈ 0
El detector ve un salto muy grande y sostenido (de 0 a ~80 000 en 19 trimestres), por eso el q es
~0 (series jóvenes: `relative_growth`, ADR 0017). La verificación descriptiva no puede confirmarlo
porque no hay tendencia previa: el nacimiento de la categoría (2021Q4) **es** el cambio.

## Propuesta (no implementada; `trajectory.py` lo cambia otra sesión)
Para series con inicio posterior a `prior_years`+`recent_years`, marcar «serie nueva» (nacimiento en
2021Q4) en vez de `no_confirmada`, y recortar los ceros iniciales antes de evaluar.
