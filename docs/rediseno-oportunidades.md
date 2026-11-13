# Rediseño: de "series que cambian" a fichas de oportunidad de negocio

> Plan aprobado por David el 2026-11-06 (copiado del plan de la sesión de trabajo el 2026-11-13). Las secciones de estado de S1/S1b son históricas; el estado actual está en `docs/STATE.md`.


## Contexto

Propósito redefinido por David (2026-11-06): identificar oportunidades de negocio poco atendidas en
EE. UU., originadas en cambios económicos verificables, con demanda creciente, brecha competitiva
concreta y vía realista de entrada. Salida: una **ficha por oportunidad** con 9 dimensiones
(Fenómeno, Evidencia, Demanda, Brecha, Competencia, Dinero, Entrada, Riesgo, Confianza).

Hoy el sistema solo cubre Fenómeno y Evidencia: su unidad de análisis es el **cluster de series**, y
con eso no se pueden responder las dimensiones de negocio. Lo existente (ingesta, detectores
con FDR, origen del dato, triangulación) se conserva como **Etapa 1: auditoría de la señal**.

Decisiones de David:
1. La unidad de análisis pasa a ser la **industria NAICS**.
2. **No se descarta, se clasifica.** El valor puede estar en una barrera de entrada baja dentro de
   una industria de gigantes (concentración alta + entrada barata = oportunidad, no descarte).
3. Un modelo de lenguaje redacta las dimensiones cualitativas, solo a partir de los datos de la
   ficha, citando cada cifra y marcando su nivel de confianza.
4. Sin límite de oportunidades por semana, pero en **dos pasos**: primero un **resumen barato**
   de cada una (solo datos, sin LLM). Las fichas completas (con LLM) se generan **solo para las que
   David elija**, para no gastar tokens ni cómputo.

## Arquitectura

```
Etapa 1  Auditoría (existe)        señales con FDR + origen + triangulación
Etapa 1b Anclaje NAICS (nuevo)     cada señal → 1..n industrias NAICS, desde los datos
Etapa 2  Mercado por NAICS (nuevo) demanda, oferta, precios, competencia, dinero, entrada
Etapa 3  Clasificación (nuevo)     etiquetas por dimensión; nada se descarta
Etapa 4  Resumen semanal (nuevo)   tabla breve de todas las oportunidades (sin LLM)
Etapa 5  Ficha bajo pedido (nuevo) 9 dimensiones, LLM con citas, ✅/🟡/⚪ por fila
```

### Etapa 1b — Anclaje NAICS (principio 1: sale de los datos)
- BLS (CES/QCEW), BEA (por industria) y Census ya publican con código NAICS en sus metadatos.
- EDGAR trae SIC por empresa; se traduce a NAICS con la **concordancia oficial** de Census (tabla
  de datos versionada, no una lista de temas).
- FRED: NAICS desde el título o las notas cuando los trae; si no, se hereda del cluster vía
  correlación con series que sí tienen NAICS.
- Términos de texto: NAICS de las empresas (SIC) que los mencionan, ponderado por frecuencia.
- Salida: tabla `naics_links (run_id, object_id, naics, peso, método)`.

**Plan de la Sesión 1, según el agente (2026-11-06), en línea con lo aprobado:**
- tablas oficiales versionadas en `config/naics/` (NAICS 2022 y concordancia SIC87→NAICS de
  Census, con fuente y fecha), más una excepción acotada al guard (`naics.tables`, solo rutas);
- `src/rank/naics.py` con 4 métodos: `meta_code`, `text_code`, `cluster_inherit` y
  `sic_concordance`;
- tabla `naics_links` y columna NAICS en el reporte.

**Ajuste:** EDGAR hoy guarda el SIC a 2 dígitos y la concordancia necesita 4. Se agrega una columna
aditiva y opcional (nullable) con el SIC de 4 dígitos. Hasta volver a procesar EDGAR, los términos
quedan como "pendiente de datos". Esa reprocesada son ~13 h de Actions: es gratis y solo cuesta
tiempo, sin tokens. Se lanza después del merge.

**Estado de S1 (2026-11-06 12:15):** el [PR #35](https://github.com/davidnaranjorios/EEUU-trend-finder/pull/35)
tiene la CI en verde.
- **Tablas oficiales** en `config/naics/`, sin excepción al guard.
- **3 métodos**, cada uno con su test:
  - `codigo_en_metadatos`;
  - `heredado_cluster`;
  - `sic_concordancia`.
- **Tabla `naics_links`**, con la cobertura por fuente en el reporte.
- **EDGAR** con SIC de 4 dígitos (esquema 5).
- **ADR 0012.**

**Próximos pasos, al aprobar:**
1. Merge del PR #35.
2. Lanzar **Ingesta texto**, que reprocesa EDGAR con el esquema 5 (~13 h). Después, **Reporte**.
3. Medir la cobertura NAICS. Riesgo conocido: BLS, BEA y Census no guardan su código de industria
   en un campo propio, así que el método de metadatos solo capta un "NAICS nnnn" explícito. Si la
   cobertura queda por debajo del 80 %, una mini-sesión (Sonnet) lee el código de industria de los
   ids de cada adaptador (por ejemplo, el código de industria dentro de los ids de CES de BLS y el
   código de línea o industria en las tablas de BEA).

**Resultado real de S1 (Reporte [42218054855](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42218054855)):**
cobertura NAICS de **0 % en todas las fuentes**. FRED 146, BLS 168, BEA 12, Census 8, EDGAR 306 y
FR 199 señales, todas sin código. Además, Pages volvió a fallar por timeout (`syncing_files`,
138 archivos, 66 MB).

**Mini-sesión S1b (Sonnet, aprobada por David): arreglar la cobertura NAICS**
1. **Diagnóstico primero:** por qué ningún método produce filas.
   - ¿`rank run` llama a `naics.py` con los objetos correctos?
   - ¿Los formatos de los códigos coinciden con la tabla oficial (2–6 dígitos, rangos como
     "31-33")?
2. **BLS:** leer el código de industria del id de la serie. En CES, los caracteres de industria se
   traducen con la tabla oficial de industrias de BLS (`ce.industry` de download.bls.gov, que
   trae el código NAICS), versionada en `config/naics/`. Mismo patrón para QCEW, OES y JOLTS si
   están.
3. **BEA:** tablas por industria. Línea o industria → NAICS con la tabla oficial de BEA (en sus
   metadatos de la API o en la concordancia publicada), versionada.
4. **Census:** el parámetro NAICS del dataset o de la variable de cada serie.
5. **FRED:** además del texto, las series copiadas de BLS, BEA o Census heredan el código por
   origen (`src/rank/origin.py`, `config/data_origin.yaml`). Después, herencia por cluster.
6. **Federal Register:** sin NAICS, marcado "no aplica". Se excluye del denominador de cobertura.
7. **Tests:**
   - fixtures con ids reales de BLS, BEA y Census → NAICS esperado;
   - un test que falla si la cobertura de una fuente con código propio es 0;
   - tablas oficiales sin mapeos escritos a mano.
8. **Pages:** si "Re-run failed jobs" vuelve a fallar, partir los archivos grandes del explorador
   (menos de 10 MB cada uno) y documentar `PUBLISH_TARGET=cloudflare_pages` como alternativa.

**Verificación de S1b:** CI en verde. Después del merge, Reporte con cobertura > 0 en BLS, BEA,
Census y FRED, y la meta de ≥ 80 % sobre las fuentes con NAICS aplicable, con el informe de
cobertura real.

**Estado de S1b (2026-11-06 14:33):** el [PR #36](https://github.com/davidnaranjorios/EEUU-trend-finder/pull/36)
tiene la CI en verde y 453 tests pasan en local.

**Por qué la cobertura dio 0 %:** solo se buscaba el texto literal "NAICS nnnn", y ninguna fuente
lo escribe así.

**Cambios del PR:**
- **BLS:** copia el `naics_code` de la tabla de industrias de cada encuesta, que el adaptador ya
  descarga. Para que tenga efecto hay que volver a ingerir los metadatos de BLS.
- **BEA, Census y FRED:** reconocen más formas de escribir el código y además comparan cada
  segmento del título con el nombre oficial NAICS 2022.
- **FRED:** hereda el código por origen (duplicados) y después por cluster.
- **Federal Register:** fuera del cálculo de cobertura.
- **Tests:** uno por fuente, más uno que falla si una fuente con código propio queda en 0 %.

**Desviaciones respecto a lo pedido:**
- La tabla `ce.industry` no quedó versionada en el repo: download.bls.gov devuelve 403 desde el
  entorno del agente. El adaptador la lee durante la ingesta.
- Pages: los archivos del explorador ya pesan ≤ 2 MB cada uno, así que no se partió nada. El
  timeout viene del volumen total (138 archivos, 66 MB). Cloudflare Pages quedó documentado como
  alternativa.
- BEA no trae un campo NAICS en su API, así que su cobertura puede quedar baja.

**Próximos pasos, al aprobar:**
1. Merge del PR #36.
2. Forzar la re-ingesta de los metadatos de BLS: correr Ingesta con un crawl de BLS. Si el
   workflow no permite forzarlo, una mínima entrada `force_crawl` en `ingesta.yml`.
3. Reporte con la medición de cobertura.
4. Si Pages vuelve a fallar: pasar a `PUBLISH_TARGET=cloudflare_pages`. Necesita un token de
   Cloudflare Pages como secret, que crea David.

### Etapa 2 — Métricas de mercado por NAICS (fuentes nuevas, gratuitas)

| Dimensión | Métrica | Fuente |
|---|---|---|
| Demanda | Gasto de consumo o de compradores, empleo de quien compra | BEA PCE e insumo-producto, BLS |
| Brecha | Demanda que crece más que la oferta: precios (PPI) que suben más que la cantidad; vacantes (JOLTS); establecimientos que no crecen | BLS PPI/JOLTS, Census CBP |
| Competencia | N.º de empresas, tamaño típico, concentración (top 4/8) | Census SUSB, Economic Census |
| Dinero | Ingresos de la industria; contratos federales por NAICS | Economic Census, USAspending.gov (API pública) |
| Entrada | Tasa de entrada y salida de empresas, capital por establecimiento, carga regulatoria (menciones en el Federal Register), % de empresas pequeñas | Census BDS y SUSB, Federal Register (ya existe) |
| Riesgo | Riesgos de los 10-K del NAICS (Item 1A, ya existe), volatilidad, ciclicidad frente a recesiones | EDGAR, USREC |

Cada fuente nueva: un adaptador en `src/ingest/` sobre el contrato `SourceAdapter`
(`src/ingest/common.py`), con el mismo test de esquema e idempotencia (`tests/test_sources_contract.py`).

### Etapa 3 — Clasificación (sin descartar)
Etiquetas por oportunidad, con umbrales en `config.yaml`:
- **Demanda:** creciente / estable / cayendo.
- **Brecha:** precio > cantidad, vacantes altas, oferta estancada, o ninguna.
- **Competencia:** fragmentada / concentrada.
- **Entrada:** barata / cara (capital, regulación, escala).
- **Combinación destacada:** "industria concentrada + entrada barata" (decisión 2).
- **Confianza global:** cuántas dimensiones son ✅ demostrado, 🟡 indicio o ⚪ hipótesis.

### Etapa 4 — Resumen semanal (sin LLM, costo casi nulo)
Una tabla con todas las oportunidades: NAICS y nombre, fenómeno en una línea, etiquetas de la
Etapa 3, confianza y enlace a la evidencia. Se publica en Pages. David marca cuáles quiere en ficha.

### Etapa 5 — Ficha bajo pedido
- Disparo: workflow `ficha.yml` con entrada `naics` (o la lista que David marque).
- Las 9 filas de la foto. Cada fila tiene:
  - respuesta en una frase;
  - dato y gráfica (unidad, fuente, fecha);
  - nivel ✅/🟡/⚪;
  - "qué falta para subir de nivel".
- LLM vía **OpenRouter** (API compatible con OpenAI), con el **modelo elegido por David** en
  `config.yaml` (`ficha.model`; sin valor por defecto: si falta, no se generan fichas). Solo con los datos de la ficha en el prompt, citas obligatorias y
  validación posterior: cada cifra del texto debe existir en los datos, o la ficha no se publica
  (test).
  - Modelo liviano para redactar.
  - Brecha, Entrada y Riesgo **siempre** incluyen una parte ⚪ hipótesis explícita.
- Necesita un secret nuevo: `OPENROUTER_API_KEY`, en GitHub y en el entorno.

## Sesiones (una por bloque; agentes con modelo liviano; plan breve y aprobación en cada una)

| # | Bloque | Criterio de terminado |
|---|---|---|
| 1 | Etapa 1b, anclaje NAICS | ≥ 80 % de las señales con NAICS; test de que no hay mapeos escritos a mano (solo la concordancia oficial) |
| 2 | Fuentes de competencia y entrada: SUSB, CBP, BDS, Economic Census | Esquema e idempotencia; métricas por NAICS en R2 |
| 3 | Fuentes de demanda, brecha y dinero: PCE/IO, PPI, JOLTS, USAspending | Ídem; brecha calculada con test sintético |
| 4 | Etapa 3 (clasificación) + Etapa 4 (resumen semanal en Pages) | Toda oportunidad clasificada y ninguna descartada (test); resumen publicado |
| 5 | Etapa 5 (ficha con LLM) | Ficha de 9 filas; test de que toda cifra citada existe en los datos; ✅/🟡/⚪ en cada fila |
| 6 | Automatización (Fase 7) | Domingo: ingesta → detectores → resumen, sin intervención. Fichas solo bajo pedido. |

Fase 6 (backtest) queda después: con la unidad NAICS cambia qué se mide como acierto.

## Archivos clave
- Se reutilizan:
  - `src/ingest/common.py` (contrato de fuente);
  - `src/rank/origin.py` y `config/data_origin.yaml` (independencia);
  - `src/rank/context.py` (relacionadas);
  - `src/report/*` (sitio y barrera de copyright);
  - `src/detect/*` (señales).
- Nuevos:
  - `src/naics/` (anclaje y concordancia);
  - `src/ingest/{susb,cbp,bds,econcensus,usaspending}.py`;
  - `src/market/` (métricas y clasificación);
  - `src/opportunity/` (resumen y ficha);
  - `.github/workflows/ficha.yml`;
  - ADR 0012 (rediseño), ADR 0013 (LLM y citas).

## Verificación
- Cada sesión: `uv run ruff check . && uv run pytest` y la CI en verde.
- Bloques 1–3: corrida real en Actions y "Verificar R2" con conteos por NAICS.
- Bloque 4: el resumen publicado lista todas las oportunidades con sus etiquetas.
- Bloque 5: una ficha real generada para una oportunidad elegida por David, revisada fila por fila
  contra los datos.
