# Estado operativo — EEUU Trend Finder

Actualizado el 2026-11-13. Reglas y metodología: `CLAUDE.md`; decisiones: `docs/decisions/`.
Pendientes y problemas conocidos: [`docs/pendientes.md`](pendientes.md).

## Qué corre hoy
- Ingesta (FRED, BLS, BEA, Census) y de texto (EDGAR, Federal Register) en GitHub Actions,
  con los datos en Cloudflare R2.
- Detectores numéricos y de texto con FDR (BH, BY de comparación) en Actions.
- Ranking, triangulación y reporte semanal estático, publicado en GitHub Pages
  (`.github/workflows/reporte.yml`).
- PRs #42–#58 mergeados.

## En curso
- Primera ficha de oportunidad (residencias de cuidado de adultos mayores): en preparación en
  otra sesión. Es la prioridad antes de seguir afinando la estadística.

## Pausado
Ver `docs/pendientes.md` (p. ej. `regime` con 0 señales, BLS que no completa la ingesta,
rediseño por NAICS).

## Navegación del código
Grafo de Graphify para consultar el código con pocos tokens: `bash scripts/setup_graphify.sh`
y `graphify query "..."` (ver `CLAUDE.md` y `docs/agent-context.md`). No se publica página del grafo.

## Verificación
Comandos: `uv sync`, `uv run ruff check . && uv run ruff format --check .`, `uv run pytest`.
Este archivo no certifica el resultado de la última corrida; mira CI y las ejecuciones de Actions.
