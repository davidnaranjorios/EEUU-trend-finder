# ADR 0030 — Capa 3, pregunta 1: «¿qué cambió realmente?» con reglas fijas

Fecha: 2026-11-18 (aprobado por David; Sesión 2 de `docs/plan-segunda-etapa.md`).

## Decisiones
1. **Tabla** `processed/rank/phenomenon_change`, clave `(run_id, phenomenon_id, change_type)`:
   `status` (`si`/`no`/`sin_dato`), `value`, `unit`, `threshold` (texto con el umbral usado), `period`,
   `source_ref`, `note` (las cifras que deciden) y `phrase` (la frase del fenómeno, repetida en cada
   fila). `src/rank/change.py`; `uv run python -m src.rank change [--run-id ID]`; `rank run` lo corre
   después de las dimensiones. El validador rechaza tipo/estado inválido, `sin_dato` con valor y
   `si`/`no` sin valor. `change_type` es un `Enum` (`schemas.ChangeType`), no una lista de temas.
2. **Cinco tipos más una fila auxiliar** (`recuperacion`, sexta fila):
   - **cantidad**: series de cantidad no poblacional, o de valor con efecto real positivo
     (`real_effect > 0`), con trayectoria `acelera` o `gira_al_alza` (ADR 0025). `frena_caida` y
     `rebote_bajo_pico` NO cuentan: van a `recuperacion`. `sin_dato` si el fenómeno no tiene ninguna
     serie de ese tipo.
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
