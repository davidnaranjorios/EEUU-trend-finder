# CLAUDE.md — Sistema de descubrimiento de tendencias (EE. UU.)

## Propósito

Construir un sistema que explore grandes volúmenes de datos económicos y de texto de EE. UU.
sin hipótesis previas y entregue, cada semana, un ranking de candidatos a tendencias
estructurales (de varios años), con la evidencia que respalda cada uno.

El sistema propone candidatos. La interpretación final la hace un humano.

## Principios no negociables

1. **Cero temas hardcodeados.** Ningún archivo en `src/` puede contener listas de industrias,
   tendencias, palabras clave o series "de interés". La selección sale de los datos.
   `config.yaml` tampoco puede contenerlas (lo verifica `tests/test_no_hardcoded_topics.py`).
2. **El set de evaluación está aislado.** Los eventos históricos conocidos viven solo en
   `eval/known_events.yaml` y solo los lee `eval/`. Un test debe fallar si algún módulo de
   `src/` importa o lee algo de `eval/` (`tests/test_isolation.py`).
3. **Control de falsos positivos obligatorio.** Toda señal estadística pasa por corrección de
   comparaciones múltiples (Benjamini-Hochberg, q configurable, por defecto 0.05), con los
   p-values calibrados bajo dependencia según "Reglas estadísticas".
4. **Triangulación.** Un candidato solo entra al ranking final si tiene respaldo en al menos
   dos fuentes independientes (p. ej., una serie numérica más señal de texto, o dos
   agencias distintas).
5. **Sin información del futuro.** El backtest usa datos tal como estaban publicados en cada
   fecha (vintages de ALFRED). Nada de datos revisados en evaluaciones históricas.
6. **Evidencia antes que afirmaciones.** No declares una fase terminada sin mostrar el output
   de los tests y una consulta de verificación.

## Stack y convenciones

- Python 3.11+, gestión de dependencias con `uv`.
- Almacenamiento: Parquet particionado en `data/`, consultas con DuckDB.
- Calidad: `ruff` (lint y formato), `pytest`, type hints en funciones públicas.
- Configuración en `config.yaml`. Secretos en `.env` (nunca en el repo; `.env` va en `.gitignore`).
- Logs estructurados; cada ejecución registra fecha, fuentes, número de series y tiempos.
- `data/` no se versiona en git.

Comandos:

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

## Estructura de carpetas

```
src/
  ingest/      # un módulo por fuente, todos producen el esquema común
  detect/      # detectores numéricos y de texto
  stats/       # FDR, desestacionalización, utilidades
  rank/        # triangulación, deduplicación y scoring
  report/      # generación del reporte HTML
  catalog.py   # vistas DuckDB sobre data/processed (local o R2)
  eligibility.py # embudo de elegibilidad con linaje por serie
  export.py    # archivos pequeños para la futura web estática
  config.py    # carga y validación de config.yaml
  log.py       # logs estructurados y registro de corridas
  schemas.py   # esquemas comunes (pyarrow) y validación
eval/
  known_events.yaml
  backtest.py
tests/
infra/         # ingesta en GitHub Actions, sync con R2 y alternativa con VPS
data/          # raw/, processed/, signals/, exports/ (no versionado)
reports/
```

## Fuentes y reglas de acceso

Verifica siempre los límites actuales en la documentación oficial antes de implementar.

| Fuente | Uso | Notas |
|---|---|---|
| FRED / ALFRED | Series macro y sectoriales; vintages históricos | API key gratuita. Respeta el rate limit con backoff. Vintages vía `realtime_start` / `realtime_end`. |
| BLS | Empleo y salarios por ocupación e industria | API v2 con key; límites diarios y de series por consulta. |
| BEA | PIB por industria y estado | API key gratuita. |
| Census | Permisos, comercio, encuestas a empresas | API key gratuita. |
| SEC EDGAR | 10-K y otros reportes (texto) | Requiere header User-Agent con nombre y correo. Límite de solicitudes por segundo. |
| Federal Register | Regulación nueva (texto) | API pública. |

Todas las descargas deben ser incrementales e idempotentes: volver a correr no duplica datos.

## Esquemas comunes

Definidos en `src/schemas.py`; todo dato que se escribe en `data/` pasa por `validate()`.

**Series numéricas** (`data/processed/series/`), clave `(source, series_id, date, vintage_date)`:

| Campo | Tipo |
|---|---|
| source | str |
| series_id | str |
| date | date |
| value | float |
| vintage_date | date (fecha de publicación; null si no aplica) |
| frequency | str (D, W, M, Q, A) |
| units | str |
| seasonal_adjustment | str |

**Metadatos** (`data/processed/series_meta/`), clave `(source, series_id)`: `source, series_id,
title, category_path, start_date, end_date, last_updated, discontinued, copyrighted`.
`copyrighted` marca series de terceros cuyo uso no personal requiere permiso del dueño (términos
de FRED): se descargan y analizan, pero **no se publican en el reporte** sin ese permiso.

Almacenamiento: `<tabla>/<fuente>/<NN>/part-*.parquet` con `NN = sha1(series_id) % n_buckets`;
escritura idempotente y compactación (≤ 1 archivo por bucket) en `src/ingest/store.py`; vistas
DuckDB en `src/catalog.py`. Linaje: `eligibility` (por qué cada serie entra o no) y
`vintage_lineage` (por qué una serie de la muestra no tiene vintages).

**Texto** (`data/processed/text_docs/`), clave `(source, doc_id, section)`: `source, doc_id,
date, entity, section, text_hash`. Tabla derivada de frecuencias de términos por periodo
(`data/processed/term_freq/`), clave `(source, section, period, term)`: `source, section,
period, term, doc_count, total_docs`. `section` separa Item 1A / Item 7 o el tipo de documento
sin crear fuentes nuevas. Linaje en `lineage/text_ingest`. Particiones por periodo
(`<tabla>/<fuente>/<periodo>/`), no por bucket; el texto completo no se guarda (ADR 0005).

**Señales** (`data/signals/`), clave `signal_id`: `signal_id, source, object_id` (serie o término)`,
detector, window_start, window_end, effect_size, p_value, q_value, run_date`.

## Reglas estadísticas

- Trabajar con log o tasas de crecimiento; desestacionalizar si la serie no viene ajustada.
- Excluir series con menos de N observaciones (configurable), descontinuadas o sin actualizar.
- Persistencia: una aceleración cuenta solo si se sostiene varios periodos consecutivos
  (configurable por frecuencia), no por un salto aislado.
- Detectores mínimos:
  - Cambio de pendiente entre ventana reciente y ventana previa (Theil-Sen o similar,
    robusto a outliers).
  - Detección de quiebres de régimen (`ruptures`, PELT).
  - Términos emergentes: crecimiento sostenido de la proporción de documentos que mencionan
    un término, con test sobre esa proporción.
- **FDR bajo dependencia.** Los p-values estándar no garantizan el control de FDR cuando hay
  dependencia temporal y entre series (Liu, Sun & Ke 2024). Por eso:
  - El p-value de cada prueba se calibra con **block bootstrap / permutación por bloques**
    (por defecto, stationary bootstrap; longitud de bloque configurable o automática), no
    con permutación simple, que destruye la autocorrelación.
  - Para preservar la dependencia entre series, se remuestrean **los mismos índices de
    bloques de tiempo para todas las series de una familia** a la vez.
  - Sobre esos p-values se aplica **Benjamini-Hochberg** (método por defecto).
  - **Benjamini-Yekutieli** se calcula y se reporta solo como **comparación** (cuántas señales
    sobreviven también a BY); no es una opción por defecto ni decide el ranking.
  - Familia de FDR: una por detector y por corrida. Cada señal registra contra cuántas
    pruebas se corrigió.
  - Knockoffs generalizados (Liu, Sun & Ke 2024) quedan como mejora futura.
- Deduplicación: agrupar series muy correlacionadas o derivadas de la misma fuente
  (p. ej., la misma variable en distintos estados o unidades) para que un fenómeno no aparezca
  cientos de veces. El ranking muestra clusters, no series sueltas.
- La relación entre términos de texto y series se establece por similitud semántica entre el
  término y los títulos/metadatos de las series, nunca por mapeos escritos a mano.

## Fases y criterios de terminado

Una fase por sesión. Commit al cerrar cada fase.

### Fase 0 — Andamiaje y guardarraíles
`uv`, ruff, pytest, `config.yaml`, `.env.example`, `.gitignore`, logging estructurado,
validadores de esquema y CI (lint + tests).
**Terminado cuando:** `test_src_never_touches_eval` y `test_no_hardcoded_topics` pasan sobre el
repo y fallan sobre archivos trampa; los validadores de esquema rechazan datos inválidos;
`ruff` y `pytest` en verde local y en CI.

### Fase 1 — Ingesta FRED
Descarga masiva de metadatos y observaciones, con actualización incremental.
**Primer paso:** evaluar `fredapi` y `pyfredapi` (cobertura de ALFRED/vintages, recorrido del
árbol de categorías, rate limit/backoff, modo incremental, mantenimiento y licencia) frente a
un cliente propio, y documentar la decisión. Hecho: cliente propio, ver
`docs/decisions/0001-cliente-fred.md`.
Uso: `uv run python -m src.ingest.fred crawl | crawl-meta | observations [--category ID] |
download | vintages [--ids-file F] | update | pick-category`, `uv run python -m src.eligibility
run | funnel`, `uv run python -m src.export` (con tope de tamaño, `exports` en config.yaml) y
`uv run python -m src.ingest.store compact` (un Parquet por bucket; lo corre `sync_r2.sh` antes
de subir). Almacenamiento y vintages: ADR 0002;
infraestructura (GitHub Actions + R2): ADR 0004 y `docs/infra/github-actions.md`
(alternativa con VPS: ADR 0003).
**Terminado cuando:** puedo consultar en DuckDB todas las series de una categoría y una segunda
ejecución no duplica filas (test).

### Fase 2 — Detector numérico + FDR
Hecho: detectores `slope` (Theil-Sen, persistencia dentro del estadístico) y `regime` (CUSUM;
PELT solo fecha), stationary bootstrap con índices compartidos y post-blackening AR(1), cola
GPD para el piso del p, BH decide y BY compara. Diseño y mediciones: ADR 0007.
Uso: `uv run python -m src.detect run [--shard I/N --run-id ID] | fdr --run-id ID | report`;
en Actions, `.github/workflows/detectores.yml`. Tablas: `detector_tests` y `detector_runs`
(`processed/lineage/`), `signals` (`data/signals/`).
**Terminado cuando** (tests):
- sobre datos sintéticos de ruido puro —incluyendo ruido AR(1) y **correlación entre series**—
  la tasa de falsos descubrimientos queda dentro de lo esperado por q;
- sobre datos sintéticos con tendencia inyectada la detecta;
- un salto aislado no genera señal (persistencia);
- el reporte de la corrida incluye el conteo BH y el conteo BY de comparación.

### Fase 3 — Ingesta y detección de texto (EDGAR, Federal Register)
Referencia de diseño para EDGAR: el modelo dinámico de riesgos emergentes de Hanley & Hoberg
(2019).
Uso: `uv run python -m src.ingest.text edgar | federal_register [--max-periods N]
[--periods P,...] [--sample N --processed-dir D] | summary`; en Actions, `infra/run_texto.sh`
vía `.github/workflows/ingesta-texto.yml` (secret `SEC_USER_AGENT`). Diseño: ADR 0005.
Detector de términos emergentes: `uv run python -m src.detect.text --source edgar |
federal_register` (ADR 0008; bootstrap y FDR de la Fase 2). Capa opcional de temas sobre los n-gramas: evaluar BERTrend (Boutaleb et al. 2024),
previa revisión de código y licencia.
**Terminado cuando:** genero la tabla de frecuencias por periodo y el detector marca un término
sintético inyectado con crecimiento artificial (test).

### Fase 4 — Fuentes adicionales (BLS, BEA, Census)
Usar un subagente por fuente, todos produciendo el esquema común.
Hecho: capa común `src/ingest/common.py` (contrato `SourceAdapter`, embudo neutral, estado por
unidad, presupuesto de minutos) y un adaptador por fuente; BLS por archivos masivos. Uso:
`uv run python -m src.ingest.<bls|bea|census> crawl [--full] | download | update | estimate`;
corrida semanal en `infra/sources_update.sh`. Decisiones y linaje agregado por unidad: ADR 0007.
**Terminado cuando:** cada fuente pasa el mismo test de esquema e idempotencia
(`tests/test_sources_contract.py`).

### Fase 5 — Triangulación, deduplicación y ranking
**Terminado cuando:** el reporte HTML semanal lista clusters candidatos con su evidencia
(series, gráficos, términos, q-values), ningún candidato tiene una sola fuente y ninguna serie
con `copyrighted = true` aparece en el reporte publicado (tests).

### Fase 6 — Backtest
Reconstruir el estado de los datos en fechas pasadas con vintages de ALFRED y medir:
- recall sobre `eval/known_events.yaml` y meses de anticipación respecto a la fecha de referencia;
- volumen de candidatos por corrida (si son demasiados, el sistema no sirve);
- comparación contra al menos una **línea base** (p. ej., detector sin FDR o con nulo por
  permutación), porque la recuperación retrospectiva de señales débiles es baja incluso en
  sistemas fuertes (BackTrend 2026: F1 máximo 10,1 %). El umbral de recall se fija de forma
  realista antes de correr el backtest, no después.

FRED-MD/FRED-QD y la base de riesgos emergentes de Hanley & Hoberg pueden alimentar `eval/`
como referencia o fuente de eventos, **nunca** `src/`.
**Terminado cuando:** hay un reporte de backtest reproducible con esas métricas.

### Fase 7 — Automatización
Ejecución semanal (GitHub Actions o cron) que actualiza datos, corre detectores y publica el reporte.
**Terminado cuando:** una corrida programada termina sin intervención y deja reporte y logs.

## Forma de trabajar

- Empieza cada fase en plan mode: propone el diseño, espera aprobación, luego implementa.
- Escribe los tests del criterio de terminado antes o junto con la implementación.
- Si una decisión de diseño afecta los principios no negociables, detente y pregunta.
- Al reportar resultados, incluye siempre el output real de tests o consultas.
- Si una API cambia o un límite impide el plan, documenta el problema y propone alternativas;
  no lo resuelvas en silencio con datos parciales.

## Referencias metodológicas

Verificadas abriendo la fuente el 2026-10-20 (metadatos de doi.org o texto del paper).

| Referencia | Dato usado | Estado |
|---|---|---|
| Liu, Sun & Ke (2024), "A generalized knockoff procedure for FDR control in structural change detection", *Journal of Econometrics* 239(2), 105331 | DOI [10.1016/j.jeconom.2022.07.008](https://doi.org/10.1016/j.jeconom.2022.07.008) | Verificado (doi.org) |
| Zhou et al. (2026), "BackTrend: Evaluating Scientific Weak-Signal Prediction via Backward Reconstruction", arXiv [2609.24921](https://arxiv.org/abs/2609.24921) | F1 máximo 10,1 % (set-level, juez LLM; DeepSeek-R1-0528); Coverage@10 máx. 18,5 % | Verificado (abstract y Tabla 2) |
| Hanley & Hoberg (2019), "Dynamic Interpretation of Emerging Risks in the Financial Sector", *Review of Financial Studies* 32(12), 4543–4603 | Alerta "as early as mid-2005"; DOI [10.1093/rfs/hhz023](https://doi.org/10.1093/rfs/hhz023) | Verificado (abstract y doi.org) |
| Boutaleb, Picault & Grosjean (2024), BERTrend, arXiv [2411.05930](https://arxiv.org/abs/2411.05930) | Método de temas para señales débiles; código `rte-france/BERTrend`, MPL-2.0 | Verificado 2026-11-03 (ADR 0006): no se integra por ahora |
