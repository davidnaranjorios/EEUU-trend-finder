# Pendientes (pausados el 2026-11-12 por decisión de David)

Prioridad actual: producir la primera ficha de oportunidad (residencias de cuidado de adultos
mayores) antes de seguir afinando la estadística. Lo de abajo queda en pausa hasta que David lo
retome; nada de esto está en curso.

## Aprobado por David, sin empezar

- **`regime` da 0 señales** (ADR 0022). Aprobadas el 2026-11-11 las opciones 1 + 2: familia FDR
  por detector y grilla nativa (cambia la regla de CLAUDE.md "una familia por detector y
  corrida"; registrar la aprobación al hacerlo) y `regime_escalate` a 999.999 remuestras solo para
  las series en el piso del p. Antes de aplicar: medir FDR sobre ruido AR(1) con correlación.

## Problemas conocidos, sin decisión

- **BLS no completa la ingesta.** Con el presupuesto duro (ADR 0019, PR #54) ya no cuelga el job:
  BEA y Census terminan y sincronizan, pero BLS cae por el watchdog
  (Ingesta 42952713094: `sources_update: fallaron bls(watchdog)`). Falta ver en los logs por unidad
  qué encuesta lo frena. Mientras tanto BLS conserva los datos anteriores.
- **Series jóvenes (`relative_growth`)**: con el padre por categoría (ADR 0020, PR #55) aún no se
  ha medido cuántas de las ~2.977 series jóvenes consiguen padre ni cuántas dan señal. Mirar
  `report["relative_growth"]` de la próxima corrida de Detectores.
- **Federal Register**: tras corregir los artefactos (ADR 0021) quedan 0 señales; la censura de la
  ingesta limita la sensibilidad.
- **Census QFR**: 6 series sin clase de medida; Census no publica el significado de sus
  `data_type_code` (ADR 0018).
- **Enlaces texto–serie**: 0 validados; revisar con EDGAR reprocesado (sector por SIC) antes de
  tocar código.
- **Cobertura de sector (NAICS)**: EDGAR 0 %, FRED ~13 %, BLS ~29 %, BEA ~44 %.
- **R2**: la papelera `_trash/` pesaba 10,6 GB el 2026-11-09; recomendado bajar la regla de ciclo
  de vida de 30 a 7 días (lo hace David en Cloudflare).

## Plan del rediseño (sin empezar)

Etapas 2 a 5 de `docs/plan.md`: métricas de mercado por NAICS (SUSB, CBP, BDS, Economic Census,
PPI/JOLTS/PCE, USAspending), clasificación, resumen semanal y ficha con LLM vía OpenRouter
(necesita el secret `OPENROUTER_API_KEY` y el modelo que elija David). Se decide después de que
David revise la primera ficha hecha a mano.
