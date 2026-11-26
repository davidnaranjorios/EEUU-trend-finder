# ADR 0034 — Sesgo post-COVID: clase «recuperación»

Fecha: 2026-11-26. Aprobado por David en su revisión de la corrida `19979e2620e64e45beae7b9f420c39ab`.

## Problema
Una serie que se recupera de un pozo (p. ej. el empleo de residencias de cuidado tras 2020) pasa por
«cambio» frente a su ventana previa, aunque solo vuelva a donde estaba. La etiqueta `rebote_bajo_pico`
ya lo describía por serie, pero el ranking no lo usaba.

## Decisiones
1. **Prueba de tendencia pre-choque** (`trajectory.preshock_trend_gap`). Además del pico previo, se
   compara el último nivel con la extrapolación del ajuste log-lineal de los `preshock_years` (5)
   años anteriores al inicio de la última recesión (USREC, serie de referencia aprobada; ninguna fecha
   en el código: para la corrida actual son 2015–2019). «Bajo la tendencia» = más de
   `preshock_tolerance_pct` (2 %) por debajo. Sin recesión en el rango (`max_back_years`) o sin
   ventana completa, la prueba no se evalúa (`None`). Config: `rank.trajectory.preshock_*`.
2. **Marca `recuperacion` por serie** = bajo el pico previo **o** bajo la tendencia pre-choque
   (`rank_evidence.recuperacion`, nullable). Las etiquetas de trayectoria no cambian.
3. **Clase del cluster y del fenómeno** (`rank_candidates.clase`, `rank_phenomena.clase`, nullable
   en particiones anteriores): `recuperacion` si **todas** sus series de cantidad (no poblacionales)
   tienen la marca; si no, `estructural`. Las series de valor nominal no deciden la clase (su nivel
   nominal no es comparable con el pico); la fila «Recuperación» de «¿qué cambió?» sí deflacta las de
   producción.
4. **Fuera del ranking principal.** Un candidato (cluster o fenómeno) de clase `recuperacion` conserva
   `status = candidato` y su ficha, pero `published = false`, sin `rank` ni `score`, y no cuenta para
   los percentiles del score. Si es triangulado, el reporte lo lista en «en observación» marcado
   «Recuperación»; si es de un solo origen y cumple la lista, también aparece marcado.
5. **Fila «Recuperación» de «¿qué cambió?»** (`change.classify_quantity`): considera **todas** las
   series de cantidad del fenómeno (empleo incluido, aunque sea insumo) y las de producción
   deflactadas; es «sí» si alguna tiene etiqueta `frena_caida`/`rebote_bajo_pico` o la marca
   `recuperacion`. «Cantidad» sigue contando solo producción.

## Condición añadida al ranking final (principio 4 de CLAUDE.md)
Esto agrega una condición al ranking final: además de ≥ 2 fuentes independientes, el candidato no
puede ser de clase `recuperacion`. Aprobada por David el 2026-11-26. No toca detectores, FDR ni
calibración.

## Límites
- La tendencia pre-choque supone que la tendencia de los 5 años previos habría continuado; es una
  referencia descriptiva, no un contrafactual. Una serie plana o a la baja antes del choque (como el
  empleo 6231) solo es recuperación por el pico.
- La clase exige que todas las series de cantidad estén en recuperación; un fenómeno sin series de
  cantidad evaluables queda `estructural`.
