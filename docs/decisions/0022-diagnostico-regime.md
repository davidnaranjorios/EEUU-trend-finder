# ADR 0022 — Diagnóstico: `regime` con 0 BH en 260.772 pruebas

Estado: diagnóstico (2026-11-11). **No cambia ninguna calibración.** Las opciones de abajo
necesitan la aprobación de David porque tocan la definición de la familia FDR o el costo.

## Pregunta

La corrida del 2026-11-10/11 (run 42879132598) dio `regime`: 0 BH en 260.772 pruebas
(p: `empirical` 258.251, `empirical_esc` 2.343, `gpd_esc` 178); el reporte avisa «posible falla».
Antes (2026-11-03, solo FRED): 304 BH en 48.699 pruebas. ¿Error de código o resultado legítimo?

## Respuesta corta

No hay un error del CUSUM ni de la escalación. El p mínimo alcanzable (≈ 7e-6) es unas 36 veces
mayor que el que BH exige con m = 260.772 (0,05/m = 1,9e-7 para el primer rango). Es una
consecuencia del piso de resolución del p más el salto de m (×5,4), no una falla de detección.

## Evidencia

**Reporte de la corrida real** (`signals/reports/2026-11-11_d09cc192…json`, regime):
`p_min = 6,92e-6`, `bh_min_p_needed = 1,92e-7`, `p_floor_blocks_bh = true`. Pruebas por
frecuencia: A 182.622 (70 %, BLS anual), M 38.882, Q 34.189, D 4.375, W 704. Slope, en la misma
corrida, sí descubre (319 BH) porque su cola GPD sobre 999 réplicas llega a 1e-12.

**Tubería real sobre datos sintéticos** (`eval/diagnostico_regime.py`; trimestral, n = 60, B = 999,
escalación de `config.yaml`; una sola familia: ruido AR(1) mayoritario + quiebres de media):

| Quiebre (sd) | p mediano | Método | BH con m = 48.699 | BH con m = 260.772 |
|---|---|---|---|---|
| 1 | 0,17 | empirical | 0 | 0 |
| 2 | 1,5e-3 | empirical_esc | 0 | 0 |
| 3 | 5,5e-5 | gpd_esc / empirical_esc | 0 | 0 |
| 5 | 1,0e-5 | gpd_esc / empirical_esc | 13 de 20 | 0 de 20 |

- El CUSUM y la escalación **sí detectan** quiebres fuertes: p cae hasta el piso. El ruido puro
  (AR(1) φ = 0 y 0,5) casi no produce p pequeños (p mínimo 1,6e-3 en 600 series; 2 % con p < 0,05).
- El p mínimo **sigue a 1/(B_esc+1)**: con B_esc = 99.999 el p mediano de un quiebre de 5–10 sd es
  1e-5 y el mínimo 5e-6; con B_esc = 999.999 baja a 3e-6 y 1e-6 (4e-7). La cola GPD sobre el nulo
  escalado no extrapola mucho más allá del piso empírico: el estadístico está acotado (≈ √n/2) y
  la forma se fuerza a ≥ 0 (cola exponencial, conservadora).
- Efecto en BH: con B_esc = 99.999, 0 de 12 quiebres de 10 sd pasan con m = 260.772; con
  B_esc = 999.999, pasan 7 de 12.
- **Por qué antes había 304 BH:** con m = 48.699 BH exige p ≤ 3,1e-4 para k = 304. Esos
  descubrimientos tenían, por inferencia (no están los p de esa corrida en el artefacto), p del orden de 1e-4–3e-4. Con m = 260.772 exige p ≤ 5,8e-5 para k = 304
  y p ≤ 1,9e-7 para el primero: ninguna serie lo cumple. El 70 % de m son anuales (BLS) que
  nunca aportan señales y diluyen la corrección.
- El nulo de CUSUM se arma sobre la serie que contiene el quiebre; medido con 20 series por fuerza,
  restar las medias por tramo (oráculo) casi no cambia el p (2e-4 en ambos para ≥ 3 sd), así que
  no es la causa. (Hallazgo del 2-sd: 1,5e-3 con quiebre frente a 2e-4 sin él; poca potencia
  en quiebres moderados, no una falla.)

## Veredicto

Legítimo dentro del diseño actual: el piso de p, no un bug, bloquea a BH. El aviso «posible
falla» del reporte (`p_floor_blocks_bh`) es correcto; compara solo el primer rango, la condición
completa es p_(k) ≤ 0,05·k/m para algún k.

## Opciones (requieren aprobación; no se aplican aquí)

1. **Familia FDR por detector y grilla** (A, M, Q, W, D): m baja a ~39.000 en M y ~34.000 en Q,
   el nivel donde el detector sí descubría. Cambia la definición «una familia por detector y por
   corrida» de CLAUDE.md (regla estadística): decisión de David.
2. **Más réplicas en la escalación** (`detect.regime_escalate` 99.999 → 999.999, solo para pruebas
   que quedan en el piso): barato en código, ~10× en CPU solo de esas ~2.500 series.
3. **Cola GPD que permita forma < 0 / p analítico acotado** para el CUSUM (distribución de Kolmogorov
   como cota), a validar contra FDR bajo AR(1) y correlación. Mayor riesgo de calibración.
4. Excluir series anuales cortas de la familia de regime (n = 15, potencia nula): reduce m ×3,
   pero es un filtro de elegibilidad que cambia el universo.

Recomendación: 1 y 2 juntas, midiendo antes FDR en ruido AR(1) (φ 0,3/0,7/0,9) con correlación
entre series, como se hizo para slope (ADR 0007).

## Reproducir

`uv run python -m eval.diagnostico_regime --n-noise 300 --n-break 20 [--escalate 999999] [--shifts 5,10]`
y `tests/test_diagnostico_regime.py` (el detector ve el quiebre, el p queda en el piso, BH falla
con m = 260.772 y funciona con pocas pruebas).
