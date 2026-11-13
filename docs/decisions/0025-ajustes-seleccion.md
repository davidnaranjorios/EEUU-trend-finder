# ADR 0025 — Ajustes de selección: trayectoria, marcas que no filtran, NAICS por título, inflación faltante

Fecha: 2026-11-13. Aprobado por David el 2026-11-13 (verificación `docs/verificacion-seleccion-2026-11-13.md`,
arreglos A, D, E y la parte de C que no cambia el ranking). El número 0024 lo usa la rama
`claude/bloque2-mercado`, por eso este es el 0025.

## Decisiones
1. **Trayectoria** (`src/rank/trajectory.py`), por serie y por fenómeno, solo de los datos.
   CAGR de la tendencia previa (`rank.trajectory.prior_years` años antes del ancla) contra el de la
   ventana reciente (ancla → último dato). El ancla es el inicio de la última recesión de USREC
   (serie de referencia ya aprobada) dentro de `max_back_years`; sin recesión, `recent_years`
   antes del último dato. El pico previo es el máximo en `peak_years` antes del ancla. Etiquetas:
   `gira_al_alza`, `frena_caida`, `rebote_bajo_pico`, `acelera` (definiciones en el módulo).
   `frena_caida` **no cuenta como «sube»** (`phenomena.direction_of`): un cluster así queda «mixto»
   y no se une a fenómenos al alza. El fenómeno toma la etiqueta más frecuente de sus series.
   Columnas aditivas y nullable: `rank_evidence.trajectory`, `rank_phenomena.trajectory`.
2. **Marcas que NO filtran** (`src/rank/marks.py`), en `rank_candidates` y `rank_phenomena`:
   `sin_evidencia_demanda` (ni cantidad no poblacional ni valor con efecto real del mismo signo) y
   `origen_posiblemente_compartido` (eslabón de origen común entre fuentes distintas o posibles
   copias). Se muestran en el reporte. La mediana y la media recortada del IPC del Fed de Cleveland
   (`^(MEDCPI|TRMMEANCPI)\w*SFRBCLE$`) se registran en `config/data_origin.yaml` con origen BLS.
3. **NAICS por título** (`rank/naics.py`, `nombre_oficial`): un tramo solo ancla si todas sus
   palabras forman nombres oficiales completos (o una medida seguida de coma, «All Employees,
   Construction»). «Housing and utilities» y «Housing, utilities, and fuels» ya no reciben NAICS 22.
4. **Mes de inflación faltante.** El CPI de octubre de 2025 no se publicó. `fill_gaps` interpola
   (log-lineal) huecos aislados del deflactor hasta `rank.inflation_effect.max_gap_months` (1) y
   marca `rank_evidence.inflacion_estimada` cuando la ventana del efecto real usa un mes estimado.

## Límites
- La trayectoria es descriptiva; no entra al score ni al detector (el arreglo B, excluir la
  recesión de la pendiente previa de `slope`, sigue pendiente: exige remedir el FDR, ADR 0007).
- La interpolación solo cubre huecos de ≤ `max_gap_months`; uno más largo deja el periodo sin dato.
- La exigencia de demanda al ranking final (arreglo C) NO se implementa aquí: solo la marca.
