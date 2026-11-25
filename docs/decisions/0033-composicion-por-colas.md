# ADR 0033 — «Composición del mercado» por colas de la distribución entre industrias

Fecha: 2026-11-24 (aprobado por David; ajuste a la Sesión 2). Modifica la regla de composición del ADR 0030.

## Problema
En la corrida de referencia (`19979e2620e64e45beae7b9f420c39ab`, 2026-11-23), «composición» salió
`si` en los 6 fenómenos con industria. Bastaba 1 de 4 criterios con umbrales fijos en pp, y entre
censos casi toda industria cambia algo: la regla no discriminaba.

## Decisión
1. **Cada criterio se compara con la distribución de ese mismo cambio** en todas las industrias del
   mismo nivel NAICS (dígitos; los rangos «31-33» forman su nivel) y del mismo periodo, en
   `market/naics_metrics`:
   - peso de subindustrias: mayor |cambio de peso| de los hijos directos (CBP) en el par de años del objetivo;
   - concentración: |Δ CR4| y |Δ CR8| entre censos (un solo criterio: cuenta si cualquiera está en la cola);
   - empresas pequeñas: |Δ del %| entre censos;
   - entrada o salida: |tasa de la industria − tasa de la economía total (`00`)| del mismo año
     (un solo criterio: cuenta si cualquiera está en la cola).
2. **Cuenta si cae en la cola**: |cambio| ≥ cuantil `composition_tail_quantile` (0.8) de esa
   distribución. Sin pares suficientes (`composition_min_peers`, 10) el criterio no se evalúa.
3. **«Composición» = `si`** si al menos `composition_min_criteria` (k = 2) de los 4 criterios están en
   la cola. `value` = criterios en la cola; `note` lista cada cifra con su percentil y el corte.
4. **`percentile`** (columna nullable nueva de `phenomenon_change`): percentil del k-ésimo criterio
   más alto; es ≥ 80 si y solo si el fenómeno cumple la regla (salvo empates).
5. Se eliminan de `config.yaml` los umbrales fijos `child_naics_shift_pp`, `concentration_shift_pp`,
   `small_share_shift_pp` y `entry_exit_gap_pp`. Quedan 0.8 y k = 2, que son de diseño, y no se
   ajustaron mirando ningún fenómeno.

## Límites
- La distribución incluye a la propia industria y a todas las del nivel, también las de poco peso.
- Con pocas industrias en el nivel (p. ej. 2 dígitos) la cola es gruesa: 0.8 de ~20 es el top 4.
- Un criterio no evaluable (p. ej. 6231 tiene un solo hijo NAICS) no cuenta como «no»: se omite y la nota lo dice.
