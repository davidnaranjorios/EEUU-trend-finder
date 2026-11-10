# ADR 0015 — Etiqueta «efecto inflación» por cluster

Fecha: 2026-11-10. Aprobada por David el mismo día. Análoga al efecto de tasas (ADR 0013, sección
del efecto de tasas); no cambia ningún principio no negociable.

## Problema
Un cluster de series nominales (salarios, ventas, valores en dólares corrientes) puede «acelerar»
solo porque subió la inflación, no porque haya una tendencia estructural propia.

## Decisión
- **Regresor:** la inflación del deflactor ya aprobado, `context.reference_series.deflator`
  (CPIAUCSL). **Sin claves nuevas** en `config.yaml` ni ampliación del guard: el deflactor ya es
  una serie de referencia metodológica que no entra a detectores ni al ranking (test).
  Inflación del periodo de la grilla = suma de las variaciones logarítmicas mensuales del índice
  dentro del periodo (solo periodos completos).
- **Mismo motor que el efecto de tasas** (`src/rank/inflation_effect.py` llama a
  `rate_effect.evaluate`): regresión del crecimiento del cluster sobre la inflación y sus
  rezagos 0..`max_lag`; R² con p por stationary bootstrap de índices compartidos por familia,
  cola GPD, BH sobre los clusters (q = `fdr.q`); condición de diferencias (el mismo modelo en Δg
  sobre Δπ, con su propio bootstrap y su propia familia BH) para descartar el ajuste espurio entre
  dos series persistentes; y prueba `slope` sobre el residuo.
- **Marca** `inflation_effect` si la relación es significativa en niveles y en diferencias, el
  cambio de pendiente original es significativo y, sin la inflación, el efecto cae ≥ `min_drop`
  (0,5) o deja de ser significativo. `inflation_share` = fracción del cambio de pendiente que
  explica la inflación.
- Sin el nivel de la tasa (`include_level` falso): la inflación ya es una tasa de cambio.
- **Solo etiqueta.** Nunca filtra ni cambia el score. Aparece en la tarjeta del candidato y en el
  glosario. Un cluster puede llevar a la vez efecto de tasas y efecto inflación.
- **Datos:** `clusters.inflation_effect/inflation_share` y `rank_candidates.inflation_effect/
  inflation_share/inflation_q/inflation_n_tests` (columnas aditivas y nulas; NULL = no evaluado).
  Resumen en `rank_runs.params_json.summary.inflation_effect`: evaluados, marcados y motivos de
  no evaluación (`sin_deflactor`, `sin_ventana`, `serie_corta`, `pocos_periodos_con_tasa`,
  `sin_crecimiento`).
- **Configuración:** `rank.inflation_effect` (`max_lag`, `min_drop`, `seed`, `min_rows_by_grid`).

## Límites
- Los datos reales no se han corrido: cuántos clusters se marcan se verá en la próxima corrida
  (`summary.inflation_effect`).
- Una serie que **es** un índice de precios (componentes del IPC) se marcará: es lo esperado; la
  etiqueta dice que su aceleración es inflación.
- Solo mira `slope`: un cluster con señal solo de `regime` no se marca (igual que el efecto de
  tasas).
- Una serie real (ya deflactada) no depende de la inflación y no se marca; la inflación y la tasa
  de corto plazo están correlacionadas, por lo que un cluster puede recibir ambas etiquetas.
