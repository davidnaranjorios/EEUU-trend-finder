# ADR 0029 — Capa 2: las 9 dimensiones como tabla de cifras por fenómeno

Fecha: 2026-11-17 (aprobado por David; Sesión 1 de `docs/plan-segunda-etapa.md`).

## Decisiones
1. **Tabla** `processed/rank/phenomenon_dimensions`, clave `(run_id, phenomenon_id, dimension, metric)`.
   Cada fila: valor numérico y/o texto, unidad, periodo, fuente (tabla o serie de origen), nivel y NAICS
   usado. Se calcula para los candidatos y los de la lista en observación (`src/rank/dimensions.py`).
   Comando `uv run python -m src.rank dimensions [--run-id ID]`; `rank run` también lo ejecuta.
2. **Niveles:** `directo` ✅ (dato del NAICS del fenómeno), `indirecto` 🟡 (otro nivel NAICS o dato
   indirecto), `sin_dato` ⚪ (la fila existe, sin valor; el validador rechaza un valor con ⚪). «Confianza»
   cuenta los tres niveles del resto de filas.
3. **Industria del fenómeno:** el código más específico cuyo peso en `naics_json` es ≥
   `rank.dimensions.naics_min_weight_share` del mayor. Si falta el dato se sube un nivel (6→4→3→2
   dígitos) y la cifra queda 🟡 con `naics_used`.
4. **Precio frente al deflactor:** el índice de precios del productor se encuentra por el código NAICS del
   id (`PCU<industria><producto>`, la serie de la industria completa repite el código), con
   `naics.id_codes`; nunca por lista. Sin ese código exacto se usa un ancestro o la subindustria más
   amplia (🟡). Base de la comparación: inicio de la última recesión (USREC, igual que la trayectoria).
5. **Brecha de empleo:** nivel actual de cada serie de cantidad del fenómeno frente a su máximo en los
   `peak_years` previos a esa recesión.
6. **Riesgo:** términos de la sección que casa con `risk_section_regex` (Item 1A) y cuyo NAICS
   (`naics_links`, concordancia SIC→NAICS oficial) coincide con la industria o un ancestro.
7. **Reporte:** sección «Antecedentes (9 dimensiones)» en cada fenómeno. Las filas de series con copyright
   (`public = false`) no llegan al sitio; la barrera de `src/report/guard.py` no cambia.
8. **Nombres de dimensión** como `Enum` (`schemas.Dimension`): no son temas ni series de interés, y el
   guard de listas literales no se toca.

## Fuera de alcance
No cambia detectores, FDR, triangulación ni score. Sin IA, sin conclusiones redactadas (Sesiones 2 en adelante).

## Límites conocidos
- «Ingresos de la industria» usa el censo económico (`market/naics_metrics`); las series trimestrales
  (Census QSS) entran solo si son evidencia del fenómeno.
- La base temporal de la brecha de precio es la recesión, no una fecha fija; difiere de la ficha manual
  (2019-01), que fue una elección a mano.
