# Estado operativo — EEUU Trend Finder

> Snapshot inicial para transferencia entre Claude Code y Codex. **No constituye una auditoría de ejecución**. Actualizar después de tareas significativas y mantenerlo breve.

## Propósito
Detectar candidatos a tendencias estructurales en datos económicos y de texto de EE. UU. sin listas predeterminadas de temas, con evidencia rastreable, control estadístico y revisión humana. Principios completos: `CLAUDE.md`.

## Fuente de verdad
- Restricciones, stack y metodología: `CLAUDE.md`.
- Decisiones técnicas: `docs/decisions/` (ADR).
- Implementación: `src/`, `eval/`, `tests/`, `config.yaml`.
- Estado de un cambio: PR/commit y pruebas verificadas, no el historial del chat.

## Estado comprobado de la documentación (2026-11-11)
- `CLAUDE.md` describe ingesta FRED, BLS, BEA, Census y texto; detectores, ranking, publicación y backtest.
- `CLAUDE.md` documenta el diseño y señala varios componentes como implementados, **sin verificación de ejecución en esta revisión**.
- `pyproject.toml` usa Python >=3.11, `uv`, `pytest` y `ruff`.
- No se han ejecutado tests, instalado Graphify ni generado un grafo en esta revisión de GitHub.

## Trabajo actual
Introducir memoria operativa corta y navegación por grafo opcional para Codex y Claude Code, sin cambios funcionales.

## Pendiente de validar en un entorno de ejecución
- `uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`.
- Instalar y ejecutar Graphify con `scripts/setup_graphify.ps1` (Windows) o `scripts/setup_graphify.sh` (Linux/macOS).
- Comprobar que ambos agentes leen este archivo y pueden consultar el grafo generado.
- Medir tokens de contexto inicial en sesiones equivalentes antes/después; no asumir ahorros.

## Bloqueos
El acceso a GitHub mediante conector permite preparar archivos y PR, pero no instala herramientas en el equipo del usuario ni ejecuta los tests del repo.

## Próxima acción
Ejecutar la instalación y extracción local en un entorno Codex/Claude Code con permisos de terminal, verificar los comandos y registrar los resultados aquí.

## Última transferencia
2026-11-11 — Preparación de protocolo de memoria y scripts de instalación; ver PR de esta rama. **No se ha realizado una auditoría funcional del pipeline.**

## Publicación del grafo en Pages (2026-11-12)

- Preparada una exportación HTML del JSON existente: 3.477 nodos, Graphify 0.9.107.
- `graphify export html` finalizó correctamente; el hash del JSON no cambió.
- `docs/graph/index.html` contiene el visor; el JSON no se versiona.
- El workflow `graph-pages.yml` publica solo ese directorio al cambiar en main o por ejecución manual; no instala Graphify ni extrae código.
- Pendiente: fusionar el PR, seleccionar GitHub Actions como fuente en Settings → Pages y verificar el primer despliegue. Instrucciones: `docs/graph-pages.md`.
