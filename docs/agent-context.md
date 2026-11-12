# Contexto compartido: Codex + Claude Code + Graphify

## Objetivo y límites
El ahorro principal procede de evitar releer conversaciones y documentación completa. `AGENTS.md` y `docs/STATE.md` hacen el handoff; los ADR documentan decisiones permanentes. Graphify es **índice de código** consultable y complementario; no sincroniza el historial de chats ni sustituye la lectura del código y las pruebas.

## Instalación en una copia del repositorio
Instalar `uv` si no está disponible. Luego:
- Windows PowerShell: `./scripts/setup_graphify.ps1`
- Linux/macOS: `bash scripts/setup_graphify.sh`

Los scripts usan `uv tool install graphifyy` (paquete oficial, comando CLI `graphify`) e instalan las skills con `graphify install --project` y `graphify install --project --platform codex`. Generan índice solo del código mediante `graphify extract . --code-only` (sin proveedor LLM para documentación). Los archivos de las skills generados bajo `.claude/skills/graphify/` y `.agents/skills/graphify/` pueden versionarse tras **revisarlos**; no se incluyen aquí porque no se han podido generar/verificar en un entorno ejecutable.

Comprobar `graphify --help` y `graphify query "How do ingestion and ranking connect?"` después de la extracción. Si una versión cambia la sintaxis, consultar la ayuda de la versión instalada y ajustar el script.

## Reglas de uso
1. Sesión nueva: leer `docs/STATE.md`, `AGENTS.md` y las reglas relevantes de `CLAUDE.md`; no cargar indiscriminadamente todo el historial ni todos los ADR.
2. Ante una pregunta sobre dependencias: consultar el grafo con una pregunta específica, después abrir los archivos precisos.
3. El grafo puede estar desactualizado. Validar rutas y símbolos en el código antes de implementar.
4. Registrar nuevas decisiones **duraderas** como ADR. Registrar el estado **temporal** en STATE y actualizarlo después de cada tarea significativa.
5. Evitar almacenar secretos, datos personales y conversaciones completas.

## Rendimiento y medición
El índice se reconstruye cuando cambian materialmente las fuentes, no en cada turno. `.claudeignore` excluye los resultados generados para reducir invalidaciones de cache de prompts, y `.gitignore` evita subirlos. En la misma tarea de prueba, comparar: tokens de entrada total, tokens cacheados/no cacheados cuando estén disponibles, tiempo hasta primera edición útil y precisión del resultado. No confundir ahorro de contexto con reducción proporcional de factura (el caching afecta el coste).

## Trabajo concurrente
Usar ramas o worktrees independientes; compartir el estado solo mediante commits/PR y reconciliarlo después de merges. No actualizar simultáneamente STATE desde dos ramas sin revisar conflictos.

## Referencia
Graphify Labs: https://github.com/Graphify-Labs/graphify — README e instrucciones de instalación. Su integración depende de la versión disponible y del entorno de ejecución.
