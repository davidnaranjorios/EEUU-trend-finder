# ADR 0018 — Demanda real: `valor` deflactado y cobertura de los efectos

Fecha: 2026-11-10. Aprobada por David el mismo día (a petición de la sesión coordinadora, tras la
corrida 3361573d). Corrige la intención de «evidencia de demanda» (ADR 0016) y del efecto
inflación (ADR 0015); no cambia ningún principio no negociable ni el ranking.

## 1. `valor` nominal ya no cuenta como demanda por sí solo
**Problema (medido por la coordinación):** la lista «en observación» tenía 32 fenómenos, casi todos
balances de la Fed (Z.1) con solo series `valor`. Un monto en dólares corrientes sube con la
inflación aunque la cantidad no cambie.

**Decisión** (`src/rank/real_effect.py`, `src/rank/watchlist.py`):
- Para cada serie `valor` con señal se mide el MISMO cambio que vio el detector sobre el
  crecimiento **deflactado**: g_real = g − π, con π la inflación del deflactor aprobado
  `context.reference_series.deflator` (CPIAUCSL) en la grilla de la serie. `real_effect` =
  (media de g_real en la ventana de la señal − media en la ventana previa de igual largo) /
  escala robusta del crecimiento nominal. Es el cambio de medias estandarizado que `slope`
  (Theil-Sen del nivel acumulado ≈ crecimiento medio) y `regime` (media después − antes) reportan
  como `effect_size`, así que se compara con el mismo umbral (`rank.single_origin.min_effect`).
- `valor` cuenta como demanda solo si |`real_effect`| ≥ `min_effect` y tiene el mismo signo que el
  efecto nominal. Sin efecto real medible (sin deflactor, ventana previa más corta que la de la
  señal, huecos en el deflactor, sin crecimiento preparado) no se puede afirmar que no sea inflación
  y no cuenta. Una serie ya real o encadenada se clasifica `cantidad` (ADR 0014) y no pasa por aquí.
- Motivo de exclusión nuevo `solo_valor_nominal`: hay series `valor` con señal pero ninguna cuenta y
  no hay `cantidad`. `sin_evidencia_de_demanda` queda para precios, tasas u `otro`.
- El umbral de efecto de la lista se aplica al efecto real en las series `valor`.
- **Datos (aditivos y nulos):** `rank_evidence.real_effect` (solo series `valor`) y
  `rank_phenomena.watch_real_effect` (mayor |efecto real| de las series `valor` que cuentan). El
  reporte lo muestra en la tarjeta («real, deflactado»).
- **No cambia:** el ranking principal, el score, la etiqueta `sin_evidencia_de_demanda` del ADR 0014
  (que sigue contando `valor` como no-precio) ni la regla de origen.

## 2. Cobertura de los efectos de tasas e inflación
**Medido (corrida 3361573d):** inflación 54 evaluados, 0 marcados, 73 no evaluados por
`serie_corta`; tasas igual.

**Causa, del código** (`rate_effect.evaluate`): exige `g.size ≥ 2·W + max(W, línea base)`. Con
`slope_window` M 60, Q 20, A 7 y `slope_min_baseline` 33 son 180 (M), 73 (Q) y 47 (A) periodos de
crecimiento. Los clusters llevan el crecimiento preparado de `regime` (`lookback_years = 15`):
M 180, Q 60, A 15. Por tanto solo se evalúan las series **mensuales con los 15 años completos**;
**ninguna trimestral ni anual puede evaluarse**, aunque `slope` sí las probó (su exigencia es
W + max(W, línea base) = 120 / 53 / 40, porque su línea base `g[:-W]` ya contiene la ventana
previa). Un cluster de precios (PPI, CPI, deflactores BEA) queda fuera si su representante es
trimestral o anual, o mensual con menos de 15 años; cuántos son así no se puede saber sin los
datos de la corrida (de ahí el diagnóstico de abajo).

**Qué se hace ahora (solo diagnóstico, no cambia ninguna decisión):** el resumen de la corrida
(`summary.rate_effect.coverage` y `summary.inflation_effect.coverage`) trae, por grilla y por clase
de medida del representante, clusters, evaluados, marcados y motivos; los periodos de crecimiento
exigidos hoy y por `slope`; y cuántos clusters `serie_corta` se evaluarían con la exigencia de
`slope` (`serie_corta_evaluable_with_slope_requirement`). Así la próxima corrida responde con
conteos reales si los clusters de precios están entre los evaluados.

**Propuesta (NO aplicada, para decidir):**
1. Exigir lo mismo que `slope` (W + max(W, línea base) en vez de 2·W + max(W, línea base)):
   pasarían las trimestrales de 15 años (60 ≥ 53) y las mensuales de 10 a 15 años. Las anuales
   siguen sin alcanzar (15 < 40): el efecto no se puede calibrar con tan poca historia.
2. Evaluar sobre la ventana del detector que generó la señal: para un cluster con señal de `regime`,
   el residuo se prueba con el CUSUM y su ventana (inicio PELT) en vez de `slope`. Hoy un cluster
   con señal solo de `regime` no se marca nunca (límite ya registrado en el ADR 0013).
Ambas cambian la calibración de la prueba de pendiente sobre el residuo y requieren medir su FDR
sobre ruido (como en el ADR 0013) antes de aplicarse; por eso no se aplican aquí.

## 3. Cobertura de la clase de medida
**Medido (corrida 3361573d, explorador público):** 49 series con señal en `otro`: 43 de BLS y 6 de
Census (cobertura Census 0 %, BLS 74,9 %).

**BLS, 8 series `CMU…D`:** prefijo oficial `CM` = **Employer Costs for Employee Compensation**
(ECEC), no el Employment Cost Index (`CI`) como se supuso: la página oficial de formatos de id de
BLS (https://www.bls.gov/help/hlpforma.htm) da `CMU1010000100000P` con «datatype code» en la
posición 17. Sus unidades son «Cost of compensation (Cost per hour worked)»: costo laboral por hora.
Regla: se amplía `costo_laboral_por_hora` (unidades «cost per hour [worked]»), sin prefijo; las
series ECEC en porcentaje del total ya caían en `tasa` por sus unidades.

**BLS, 35 series `LNU…` («Population Level»):** conteos de personas de la CPS. Regla nueva
`poblacion_cps` (fuente bls, prefijo oficial `LN[SU]`, título «population level») con clase
`cantidad` y **subtipo nuevo `poblacion`**, que **no cuenta como evidencia de demanda** en la lista
en observación (es demografía y no demanda de mercado; nuevo motivo `solo_poblacion`). Otras series
de la CPS (p. ej. «Employment Level») no se tocan: el patrón es solo «population level».
Decisión a confirmar por David: la regla usa el título («population level») porque la página
oficial de formatos de BLS no documenta el formato de id `LN` ni su código de condición laboral, así
que no se pudo apoyar en un código oficial; la palabra se admite en el vocabulario del test como
nombre oficial de la medida de la CPS, no como tema.

**Census, 6 series QFR (`timeseries:eits:qfr:…:data_type_code=219|322|D119`): SIN CAMBIO, siguen en
`otro`.** El significado de `data_type_code` no está publicado de forma que se pueda verificar:
`variables.json` del dataset solo da la etiqueta «item type» (sin valores ni variable `*_LABEL`), la
página de consulta de Census no define 219, 322 ni D119, y la documentación histórica del QFR
(`historic-documentation-naics.pdf`) usa otros códigos de partida (SALES, TOTASSET…) sin esos
números. Sin significado verificado no se inventa una clase (valor en dólares frente a razón). Hace
falta la tabla oficial de tipos de dato de EITS/QFR (Documento técnico del QFR en
https://www.census.gov/econ/qfr/ o la oficina del QFR); con ella, una regla por
`data_type_code` es trivial. Queda como test (`test_census_qfr_item_codes_stay_unclassified…`) para
que el día que se añada la regla el cambio sea deliberado.

**Efecto esperado en la cobertura (sin datos, solo por los ids recibidos):** las 43 series de BLS
pasan a clasificarse (8 precio/costo laboral, 35 cantidad/población): BLS 100 % de esas señales. Census
queda en 0 %. La cobertura real se mide en la próxima corrida (`summary.measure_kind_coverage`).

## Límites
- Sin corrida real todavía: cuántos fenómenos salen de la lista al exigir efecto real se ve en
  `summary.watchlist.excluded_by_reason.solo_valor_nominal`.
- El efecto real usa la ventana previa de igual largo: una serie con una señal larga (por ejemplo
  un régimen de 12 años) puede no tener ventana previa en el crecimiento preparado (15 años) y
  quedar sin efecto real medible; en ese caso no cuenta como demanda (conservador).
