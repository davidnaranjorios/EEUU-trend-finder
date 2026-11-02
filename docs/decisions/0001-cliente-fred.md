# 0001 — Cliente FRED propio en lugar de fredapi / pyfredapi

- Fecha: 2026-10-20
- Estado: aceptada (Fase 1); el modelo de vintages queda reemplazado por el ADR 0002

## Contexto

CLAUDE.md pide evaluar `fredapi` y `pyfredapi` antes de implementar la ingesta FRED. Se
leyó el código de los wheels publicados en PyPI y la documentación oficial de la API.

Límites oficiales verificados (fred.stlouisfed.org/docs/api, 2026-10-20):
- 120 solicitudes/minuto; superar el límite devuelve 429 y puede causar un bloqueo temporal.
- `series/observations`: `limit` máximo 100.000; tope de 2.000 `vintage_dates` por consulta (JSON).
- `category/series` y `series/updates`: `limit` máximo 1.000.
- `series/updates` solo cubre series actualizadas en las últimas dos semanas.
- Términos de uso: las series de terceros con "Copyright" en sus notas requieren permiso del
  dueño para uso no personal.

## Evaluación

| Criterio | fredapi 0.5.2 (Apache-2.0, may 2024) | pyfredapi 0.10.2 (MIT, jul 2025) |
|---|---|---|
| Vintages ALFRED | Sí | Sí |
| Árbol de categorías | No (sin `category/children`) | Sí |
| `series/updates` | No | Sí |
| Paginación de observaciones | No: una sola llamada; trunca en silencio sobre 100.000 filas | No: igual |
| Paginación de `category/series` | Sí (vía search) | No: trunca en silencio sobre 1.000 series |
| 429 / backoff | No | No |
| Efectos laterales | `install_opener` global de urllib | `@lru_cache` en todas las solicitudes (memoria sin límite, respuestas obsoletas) |

## Decisión

Cliente propio y delgado (`src/ingest/http.py`, `src/ingest/fred.py`) sobre `httpx`:
rate limit con margen (100/min), backoff exponencial con jitter ante 429/5xx/timeouts,
paginación verificada contra `count` (falla en vez de truncar), copia raw sin credenciales.

## Consecuencias y detalles

- **Fecha de vintage correcta.** Con el rango realtime por defecto (hoy), FRED devuelve
  `realtime_start = hoy` en todas las observaciones, lo que crearía filas nuevas cada día. Se
  pide el rango completo (`1776-07-04`..`9999-12-31`) y se guardan solo las filas vigentes
  (`realtime_end = 9999-12-31`); `vintage_date = realtime_start` es la fecha real de
  publicación del valor vigente. Las revisiones posteriores entran como filas nuevas.
- **Tope de 2.000 vintages.** Si una serie lo excede, se pide desde su última vintage
  (`realtime_floor`, guardado en el estado). Las observaciones viejas quedan fechadas en esa
  vintage, posterior a su publicación real: es conservador y nunca introduce información del
  futuro. Cada caso se cuenta (`vintage_cap_fallbacks`) y se registra en el log.
- **Frecuencias fuera del esquema** (BW, SA, ...): se excluyen y se cuentan por código.
- **Copyright:** `copyrighted` en `series_meta`; si una serie no trae notas en
  `category/series`, el crawl lo reporta en `notes_missing` en vez de suponer "sin copyright".
- **`discontinued`:** se detecta por el sufijo "(DISCONTINUED)" del título. Confirmado contra
  el catálogo real (2026-10-20, 853.316 series): el título marca 74.642 series y la etiqueta
  `discontinued` 73.618. Coinciden en 73.569. Hay 1.073 solo con título (en parte porque la
  paginación de la etiqueta se cortó) y 49 solo con etiqueta, 47 de ellas actualizadas en
  octubre de 2026: la etiqueta tiene restos viejos. Se mantiene el título, y el filtro de
  antigüedad descarta el resto.
- Tabla interna `fred_catalog` (fuera del esquema común) guarda frecuencia, unidades y
  categorías para construir las filas de `series`.

## Hallazgos de la verificación en vivo (2026-10-20/21)

- **Series que no existen en ALFRED:** algunas series de FRED (p. ej. `ATNHPIUS16984Q`)
  responden HTTP 400 "The series does not exist in ALFRED but may exist in FRED" cuando se
  pide el rango realtime completo. Con el ADR 0002 solo la muestra de vintages pide ese rango;
  esas series se cuentan en `series_errors` y quedan fuera de la muestra.
- **Tope de 2.000 vintages:** afecta a muchas series diarias y a algunas mensuales. El
  `realtime_floor` las fechaba en su última vintage, a veces el mismo día de la descarga. Lo
  reemplazan las ventanas de `series/vintagedates` del ADR 0002.
- **`tags/series` devuelve un `count` inestable** (73.850 y luego 73.196 para la misma
  etiqueta) que no coincide con las filas servidas. Para ese endpoint el paginador termina en
  la primera página corta (`trust_count=False`), en vez de fallar por "paginación incompleta".
- Ritmo real de v1: ~84 series/min con 100 solicitudes/min configuradas; el tiempo de cada
  respuesta pesa más que el tope.
