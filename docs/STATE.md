# Estado operativo — EEUU Trend Finder

Actualizado el 2026-11-17. Reglas y metodología: `CLAUDE.md`; decisiones: `docs/decisions/`.
Pendientes y problemas conocidos: [`docs/pendientes.md`](pendientes.md).
**Objetivo del producto** (aprobado por David el 2026-11-06): fichas de oportunidad de negocio de
9 dimensiones por industria NAICS; plan en [`docs/rediseno-oportunidades.md`](rediseno-oportunidades.md).

## Qué corre hoy
- Ingesta (FRED, BLS, BEA, Census) y de texto (EDGAR, Federal Register) en GitHub Actions,
  con los datos en Cloudflare R2.
- Detectores numéricos y de texto con FDR (BH, BY de comparación) en Actions.
- Ranking, triangulación y reporte semanal estático, publicado en GitHub Pages
  (`.github/workflows/reporte.yml`).
- PRs #42–#58 mergeados.

## En curso
- **Segunda etapa** (aprobada el 2026-11-17): las 9 dimensiones como respaldo de cuatro preguntas
  por fenómeno; plan y sesiones en [`docs/plan-segunda-etapa.md`](plan-segunda-etapa.md).
- Sesión 0 de ese plan: arreglos de ingesta semanal, `text_structure` en R2 y lista en observación.
- Primera ficha hecha a mano: [`docs/fichas/2026-11-residencias-cuidado.md`](fichas/2026-11-residencias-cuidado.md).

## Pausado
Ver `docs/pendientes.md` (p. ej. `regime` con 0 señales, BLS que no completa la ingesta,
rediseño por NAICS).

## Navegación del código
Grafo de Graphify para consultar el código con pocos tokens: `bash scripts/setup_graphify.sh`
y `graphify query "..."` (ver `CLAUDE.md` y `docs/agent-context.md`). No se publica página del grafo.

## Verificación
Comandos: `uv sync`, `uv run ruff check . && uv run ruff format --check .`, `uv run pytest`.
Este archivo no certifica el resultado de la última corrida; mira CI y las ejecuciones de Actions.
