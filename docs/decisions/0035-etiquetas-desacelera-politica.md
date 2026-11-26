# ADR 0035 — Etiquetas: «desacelera» y clase política/contable

Fecha: 2026-11-26. Aprobado por David en su revisión de la corrida `19979e2620e64e45beae7b9f420c39ab`.

## Decisiones
1. **Dirección mostrada.** Si la trayectoria es `frena_subida` (sigue subiendo, más despacio), la
   dirección que se muestra es **«desacelera»** (`phenomena.display_direction`). «Baja» se reserva
   para niveles que caen. La dirección guardada (`rank_phenomena.direction`: sube/baja/mixto) no
   cambia, porque agrupa fenómenos; solo cambia lo que ve el lector (reporte, lista en observación,
   fila «dirección» de dimensiones).
2. **Clase `politica_contable`.** Series de política monetaria o contables, p. ej. el balance de la
   Reserva Federal (publicación H.4.1 de FRED). El criterio es el **`release_id` de FRED** como
   metadato de agencia: `config/measure_kind.yaml` admite ahora la clave `match.release_id` (entero
   positivo) y la regla `politica_contable_h41` usa `release_id: 20`. Verificado en
   `fred_series_attrs` de R2: WLAD, WOTHLIAB, RESPPLLOPNWW y WALCL → `[20]`. No hay lista de ids de
   serie; es el mismo tipo de criterio que `release_id: 482` de `data_origin.yaml`, así que el guard de
   temas y el principio 1 no lo impiden.
3. **Exclusión.** Las series de esa clase se sacan antes de agrupar en clusters: no llegan al ranking
   ni a la lista en observación. El reporte las cuenta en la sección «Política o contable
   (excluidas)» (`rank_runs` summary `n_policy_excluded`).

No toca detectores, FDR ni calibración.

## Efecto en la corrida de referencia (112 fenómenos)
- Series H.4.1 con señal: 4 (RESPPLLOPNWW, WLAD, WOLCL, WOTHLIAB). Salen de 1 fenómeno en observación
  (`pdbf7cb4795e3`, 5 series: 4 de H.4.1 y 1 más).
- «Desacelera»: 5 fenómenos con dirección `baja` y trayectoria `frena_subida` (4 en observación:
  `p4540e0577c0d`, `pbb8893d7160d`, `pd0daad5a8761`, `pfb8cd62ff0a8`; y `pfdbb9e964d08`).
- Direcciones guardadas: sube 94, baja 16, mixto 2.
