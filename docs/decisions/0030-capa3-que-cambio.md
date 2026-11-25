# ADR 0030 — Capa 3, pregunta 1: «¿qué cambió realmente?» con reglas fijas

Fecha: 2026-11-18 (aprobado por David; Sesión 2 de `docs/plan-segunda-etapa.md`).

## Decisiones
1. **Tabla** `processed/rank/phenomenon_change`, clave `(run_id, phenomenon_id, change_type)`:
   `status` (`si`/`no`/`sin_dato`), `value`, `unit`, `threshold` (texto con el umbral usado), `period`,
   `source_ref`, `note` (las cifras que deciden) y `phrase` (la frase del fenómeno, repetida en cada
   fila) y `level` (✅ directo / 🟡 indirecto / ⚪ sin dato). `src/rank/change.py`; `uv run python -m src.rank change [--run-id ID]`; `rank run` lo corre
   después de las dimensiones. El validador rechaza tipo/estado inválido, `sin_dato` con valor y
   `si`/`no` sin valor. `change_type` es un `Enum` (`schemas.ChangeType`), no una lista de temas.
2. **Cinco tipos más una fila auxiliar** (`recuperacion`, sexta fila):
   - **cantidad** (regla vigente desde el ajuste del 2026-11-19; ver abajo): solo series de
     **producción**, con trayectoria `acelera` o `gira_al_alza` (ADR 0025). `frena_caida` y
     `rebote_bajo_pico` NO cuentan: van a `recuperacion`. `sin_dato` si el fenómeno no tiene ninguna
     serie de producción evaluable.
   - **precio**: PPI de la industria (mismo hallazgo por código NAICS del id que ADR 0029) menos el
     deflactor aprobado, ambos como CAGR desde el inicio de la ventana del fenómeno hasta el último
     dato; `si` si la diferencia supera `price_min_pp_year`.
   - **productividad**: entre los dos censos económicos (2017→2022), ingreso real (ingresos ÷
     deflactor anual medio) por trabajador o por establecimiento crece ≥ `productivity_min_pct_year`
     por año **y** el empleo (CBP; si falta, el del censo) crece a lo sumo `employment_stable_pct_year`
     por año (estable o cae).
   - **composición** (`si` si se cumple al menos uno de los evaluables): mayor cambio de peso de
     las subindustrias (hijos NAICS directos, empleo de CBP; establecimientos si no hay empleo)
     ≥ `child_naics_shift_pp` en `composition_years`; cambio de CR4 o CR8 entre censos
     ≥ `concentration_shift_pp`; cambio del % de empresas pequeñas ≥ `small_share_shift_pp`; tasa de
     entrada o salida vs la economía total (`00`) del mismo año ≥ `entry_exit_gap_pp`. `value` =
     criterios cumplidos; `note` lista cada cifra.
   - **regulación**: siempre `sin_dato`, nota «falta vincular regulación con industria».
3. **Frase** por plantilla: «Cambió: … · No cambió: … · Recuperación: … · Sin dato: …».
4. **Umbrales** en `config.yaml` (`rank.change`), fijados a priori en valores redondos; no se
   calibraron contra ningún caso. Las etiquetas de trayectoria que cuentan son del método, no temas.
5. **Reporte**: sección «¿Qué cambió realmente?» sobre «Antecedentes (9 dimensiones)» y la frase en
   una línea bajo cada fenómeno (candidatos y en observación). `source_ref` y `note` solo traen ids de
   series públicas; las series con copyright se cuentan («+N con derechos»), no se listan. La barrera
   de `src/report/guard.py` no cambia.

## Regulación: método propuesto (no implementado)
Hoy ningún documento del Registro Federal se vincula con una industria. Propuesta: similitud
semántica (los mismos embeddings locales de `src/rank/embed.py`) entre los títulos de las partes del
Code of Federal Regulations de cada documento (`cfr_references` del API) y los nombres NAICS de
`config/naics/naics_2022.csv`, con umbral nulo por parte obtenido de la distribución de similitudes
con todos los NAICS (como en los cruces término–serie). Después, contar por industria y año los
documentos que modifican las partes vinculadas y compararlo con el total. Nunca un mapeo escrito a
mano. Requiere ampliar la ingesta de Federal Register para guardar las referencias CFR.

## Límites conocidos
- **Productividad** usa ingresos de dos censos (2017 y 2022): captura un lustro, no la ventana
  del detector. El empleo de CBP y el del censo económico no coinciden en definición.
- **Composición**: CBP no armoniza los códigos NAICS entre revisiones (2012/2017/2022); los hijos
  que aparecen o desaparecen entre los dos años no entran al peso, pero un código que cambió de
  significado sí podría. Sin NAICS en el fenómeno (p. ej. financiero) quedan `sin_dato`.
- **Cantidad** acepta series de valor con efecto real positivo (nómina, ingresos reales). Si un
  fenómeno tiene solo una serie de cantidad física en recuperación y varias de valor que aceleran,
  sale `si` por las de valor; ver el PR (caso de residencias de cuidado).

## Ajuste del 2026-11-19: producción frente a insumos (aprobado por David; PR #71)
La primera versión contaba como «cantidad» cualquier serie de valor con efecto real positivo. Así
entraron nómina y gastos, que son costos o insumos y no demanda, y el empleo, que es un insumo y no la
cantidad producida. La verificación con residencias de cuidado salió «cantidad: sí» por esas series.
Era un error de la regla, no del código. Cambios:
1. **Rol por serie** (`src/rank/role.py`, `config/measure_role.yaml`): `produccion` (ingresos,
   ventas, envíos, valor real encadenado), `insumo` (nómina, gastos, empleo, horas, salarios) o
   `sin_clasificar`, por título y unidades oficiales, mismo esquema de `match` que
   `measure_kind.yaml`. Son descriptores del tipo de medida, no temas; `test_no_hardcoded_topics`
   pasa sin excepción y sin partir literales (las expresiones viven en el YAML de `config/`).
2. **Cantidad = solo producción.** Cantidad física: su trayectoria tal cual (✅). Valor: se divide
   por el índice de precios del productor (`dimensions.find_price_series`/`price_source`; promedio
   de los meses del periodo) y se etiqueta con `trajectory.classify` (mismas ventanas: tendencia
   previa vs reciente, ancla en la última recesión). Nivel ✅ si el índice es del NAICS propio de la
   serie; 🟡 si es de otro nivel NAICS (nota «índice de precios de 6231 para una serie de 623») o si
   no hay índice y se usa el deflactor aprobado (nota con su id). El NAICS propio sale de
   `naics_links` (peso mayor) o del id.
3. **Insumos → productividad.** El ingreso del censo se deflacta con el índice de precios del
   productor de la industria (🟡 si es de otro nivel o si cae al deflactor aprobado); los insumos
   del fenómeno con su trayectoria (empleo, nómina, gastos) se listan en la nota de productividad y
   nunca en cantidad.
4. **Recuperación** solo cuenta series de producción; un insumo en rebote va a la nota de
   productividad.
5. `phenomenon_change` gana la columna `level`.

Efecto en la corrida de referencia (33 fenómenos): la mayoría de los fenómenos no tiene ninguna serie
de producción evaluable (su respaldo son precios, empleo u otros insumos), así que cantidad queda
`sin_dato` en 31; es el comportamiento pedido (los insumos no son cantidad), no una pérdida de datos.

## Ajuste del 2026-11-24: composición con umbrales derivados de los datos (ADR 0033)
Los cuatro umbrales fijos en pp de «composición» y la regla «basta 1 criterio» se reemplazaron;
ver `docs/decisions/0033-composicion-por-colas.md`.
