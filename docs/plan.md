# Plan de acción: pendientes y fases 2–7

- Fecha: 2026-11-02
- Punto de partida: fases 0 y 1 terminadas y verificadas en R2:
  - 53.008 series nacionales;
  - 36,5 M de filas vigentes;
  - 2.038 series con vintages, que suman 5,5 M de filas;
  - catálogo de 853.318 series.

Una sesión por bloque (CLAUDE.md). Cada fase empieza en plan mode: Claude propone el diseño
detallado, tú lo apruebas y recién entonces se implementa.

## Cómo correr cada sesión

### Pasos comunes a todas las sesiones

1. **Abre una sesión nueva** en claude.ai/code con el repositorio `EEUU-trend-finder` y la rama
   `main`. Que sea nueva cada vez: así arranca con el contexto limpio y CLAUDE.md cargado.
2. **Revisa los prerrequisitos** del bloque (abajo). Si falta una key, créala antes; los
   secretos van siempre en GitHub → Settings → Secrets and variables → Actions, nunca en el chat.
3. **Pega el prompt del bloque** tal cual.
4. **Lee el plan** que propone Claude y responde una de estas tres cosas:
   - "apruebo";
   - "apruebo con estos cambios: …";
   - preguntas.

   Las decisiones marcadas como tuyas te las pregunta Claude en ese momento.
5. **Espera la implementación.** Al terminar, Claude tiene que mostrarte cuatro cosas. Si falta
   alguna, pídela.
   - el output real de `ruff` y `pytest`;
   - una consulta de verificación con números reales;
   - el PR abierto con la CI en verde;
   - qué quedó pendiente, si algo quedó.
6. **Haz el merge** a `main`. Puedes decirle "haz el merge" o darle **Merge** en GitHub.
7. **Marca el bloque como hecho** en la tabla de avance (al final) o pídele a Claude que lo haga
   en el mismo PR.

Si una sesión se corta a la mitad, abre otra y pega:

> Continúa el bloque <N> de docs/plan.md. Revisa el estado en la rama y en el último PR abierto
> antes de seguir.

---

## Bloque 0: pendientes menores (1 sesión)

**Prerrequisitos:** ninguno.

**Prompt:**

> Lee CLAUDE.md y docs/plan.md. Trabaja el bloque 0 en plan mode: propón el plan, espera mi
> aprobación y luego implementa. Puntos: 0.1 compactación de Parquet (primero averigua por qué
> hay 14.491 archivos), 0.2 reducir los exportes con tope de tamaño, 0.3 motivo en el linaje de
> las 5 series de la muestra sin vintages, 0.4 guía para la regla de expiración de `_trash/` en
> R2, 0.5 actualizar las acciones con aviso de Node 20, 0.6 lanzar la actualización semanal a
> mano y verificarla con "Verificar R2", 0.7 actualizar ADR 0004 y CLAUDE.md. Al final muéstrame
> los tests, el conteo de archivos en R2 antes y después, y el tamaño de los exportes.

**Tarea manual tuya:** crear en Cloudflare la regla de expiración de `_trash/` (Claude te da la
guía).

**Terminado cuando:**

| Punto | Criterio |
|---|---|
| 0.1 | Una segunda corrida no aumenta el número de archivos (test). Leer directamente desde R2 con DuckDB tarda minutos. |
| 0.2 | Los exportes respetan el tope (test). |
| 0.3 | Las 5 series sin vintages tienen su motivo en el linaje. |
| 0.4 | La regla de `_trash/` está activa y documentada. |
| 0.5 | La CI corre sin avisos de Node 20. |
| 0.6 | La corrida semanal manual queda en verde y "Verificar R2" cuadra. |
| 0.7 | El ADR 0004 y CLAUDE.md están actualizados. |

---

## Bloque 1: Fase 2, detector numérico y FDR (1–2 sesiones)

**Prerrequisitos:** bloque 0 fusionado.

**Prompt:**

> Lee CLAUDE.md, docs/plan.md y los ADR. Empieza la Fase 2 en plan mode. Antes de proponer el
> diseño final, mide el costo del bootstrap (stationary bootstrap con los mismos bloques para
> toda la familia, 999 remuestreos) para Theil-Sen y para PELT sobre una muestra de series reales,
> y propón cómo correrlo dentro de GitHub Actions (vectorizar, estadístico tipo CUSUM para la
> prueba con PELT solo para localizar, o jobs en paralelo). Escribe primero los tests del
> criterio de terminado: ruido puro, AR(1) y series correlacionadas con FDR dentro de q;
> tendencia inyectada detectada; salto aislado sin señal; conteos BH y BY en el reporte.
> Después corre los detectores sobre los datos reales y muéstrame cuántas señales salen por
> detector, por frecuencia, y la comparación BH vs BY.

**Decisión tuya en esta sesión:** cómo correr el cómputo, según lo que se mida.

**Terminado cuando:**
- los cuatro tests del criterio de la Fase 2 en CLAUDE.md pasan;
- existe una corrida real con las tablas `signals` y `detector_runs` en R2.

---

## Bloque 2: Fase 3, texto: EDGAR y Federal Register (2 sesiones)

**Prerrequisitos:** crear el secret `SEC_USER_AGENT` en GitHub con el formato
`Nombre Apellido correo@dominio` (lo exige la SEC). No necesita más keys.

**Prompt, sesión A (ingesta):**

> Lee CLAUDE.md, docs/plan.md y los ADR. Empieza la Fase 3 en plan mode, parte A: ingesta de
> EDGAR (10-K, secciones Item 1A e Item 7, referencia Hanley & Hoberg 2019) y del Federal
> Register. Verifica en la documentación oficial los límites actuales de ambas APIs. La descarga
> debe ser incremental, idempotente y por tramos reanudables dentro de GitHub Actions, con el
> User-Agent desde el secret SEC_USER_AGENT. No guardes el texto completo en R2: solo text_hash
> y la tabla de frecuencias por periodo (salvo que yo diga otra cosa). Estima volumen y tiempo
> antes de lanzar la descarga completa.

**Prompt, sesión B (detector):**

> Continúa la Fase 3, parte B: n-gramas de 1 a 3 palabras, frecuencias por periodo y detector
> de términos emergentes (crecimiento sostenido de la proporción de documentos, block bootstrap
> sobre los periodos, BH por fuente y corrida). Evalúa BERTrend (código y licencia) solo en un
> ADR, sin implementarlo. Test del criterio: un término sintético inyectado con crecimiento
> artificial se detecta.

**Decisión tuya:** si se guarda o no el texto completo. Lo recomendado es no guardarlo.

**Terminado cuando:**
- la tabla de frecuencias se genera con datos reales;
- el test del término inyectado pasa;
- la ingesta es idempotente (test).

---

## Bloque 3: Fase 4, BLS, BEA y Census (1 sesión)

**Prerrequisitos:** crear las tres keys gratuitas y cargarlas como secrets:

| Fuente | Dónde se pide la key | Nombre del secret |
|---|---|---|
| BLS | data.bls.gov/registrationEngine | `BLS_API_KEY` |
| BEA | apps.bea.gov/api/signup | `BEA_API_KEY` |
| Census | api.census.gov/data/key_signup.html | `CENSUS_API_KEY` |

Si no encuentras alguna, pídele a Claude la guía al empezar la sesión.

**Prompt:**

> Lee CLAUDE.md, docs/plan.md y los ADR. Empieza la Fase 4 en plan mode. Usa un subagente por
> fuente (BLS, BEA, Census), todos produciendo el esquema común de src/schemas.py y pasando por
> el embudo de elegibilidad. Verifica los límites actuales de cada API en la documentación
> oficial; para BLS evalúa los archivos masivos de download.bls.gov frente a la API v2. Agrega
> las tres fuentes a la corrida semanal. Criterio: cada fuente pasa el mismo test de esquema e
> idempotencia. Muéstrame cuántas series quedan por fuente y grupo.

**Terminado cuando:**
- las tres fuentes pasan el test de esquema e idempotencia;
- los datos reales están en R2 y "Verificar R2" los cuenta.

---

## Bloque 4: Fase 5, triangulación, deduplicación, ranking y reporte (1–2 sesiones)

**Prerrequisitos:** bloques 1, 2 y 3 fusionados. Decidir dónde se publica el reporte; lo
recomendado es GitHub Pages.

**Prompt:**

> Lee CLAUDE.md, docs/plan.md y los ADR. Empieza la Fase 5 en plan mode: deduplicación en
> clusters (correlación de tasas de crecimiento + similitud de títulos/metadatos), cruces
> término–serie por similitud semántica con embeddings locales de licencia abierta (nunca
> mapeos a mano, guardados en series_term_links), triangulación con al menos dos fuentes
> independientes, score con pesos en config.yaml, y reporte HTML semanal con clusters y su
> evidencia (series, gráficos, términos, q-values). El reporte incluye el dashboard: estado del
> sistema, embudo, cobertura y explorador de series. Publícalo en <GitHub Pages / lo que
> decida>. Tests: ningún candidato con una sola fuente y ninguna serie copyrighted en el
> reporte publicado.

**Terminado cuando:**
- los dos tests pasan;
- el reporte está publicado con datos reales.

---

## Bloque 5: Fase 6, backtest (1–2 sesiones)

**Prerrequisitos:**
1. **Eventos conocidos.** En una sesión previa, corta, pide:

   > Propón 20–40 eventos estructurales de EE. UU. con fecha de referencia y fuente verificable,
   > para eval/known_events.yaml. No los escribas en src/.

   Revisa la lista, edítala y apruébala; ese archivo solo lo lee `eval/`.
2. **Umbral de recall.** Fíjalo por escrito **antes** de correr el backtest. Lo sugerido es que
   al menos el 20 % de los eventos se detecten con 6 meses o más de anticipación, con menos de
   50 candidatos por corrida.

**Prompt:**

> Lee CLAUDE.md, docs/plan.md y los ADR. Empieza la Fase 6 en plan mode. El umbral de recall
> fijado es: <umbral>. eval/ genera la lista de series ligadas a known_events.yaml y llama a
> `vintages --ids-file`. Reconstruye el estado de los datos en cada fecha con vintages de ALFRED
> (sin información del futuro), corre el pipeline completo en cada fecha y mide recall, meses de
> anticipación, candidatos por corrida y comparación contra dos líneas base (sin FDR y con
> permutación simple). Entrega un reporte de backtest reproducible.

**Terminado cuando:** el reporte del backtest es reproducible, tiene las métricas y está
comparado con el umbral fijado de antemano.

---

## Bloque 6: Fase 7, automatización completa (1 sesión)

**Prerrequisitos:** bloques anteriores fusionados.

**Prompt:**

> Lee CLAUDE.md, docs/plan.md y los ADR. Empieza la Fase 7 en plan mode: la corrida semanal
> debe hacer ingesta de todas las fuentes, detectores, ranking, reporte, publicación y
> verificación sin intervención. Si no cabe en 6 h, pártela en jobs encadenados con estado en
> R2. Avisos de falla por correo de GitHub; logs y reportes conservados por fecha. Lanza una
> corrida programada de prueba y muéstrame que terminó sola y dejó reporte y logs.

**Terminado cuando:** una corrida programada termina sin intervención y deja reporte y logs.

---

## Decisiones pendientes

| # | Decisión | Antes del bloque | Recomendación |
|---|---|---|---|
| 1 | Lista de eventos conocidos | 5 | Claude propone una lista con fuentes y tú la apruebas |
| 2 | Keys de BLS, BEA y Census y el User-Agent de SEC | 2 y 3 | Crearlas justo antes de cada bloque |
| 3 | Dónde publicar el reporte y el dashboard | 4 | GitHub Pages, sin series con copyright |
| 4 | Umbral de recall | 5 | ≥ 20 % de eventos con ≥ 6 meses de anticipación y < 50 candidatos por corrida |
| 5 | Guardar el texto completo | 2 | No |
| 6 | Expiración de `_trash/` | 0 | 30 días |

## Avance

| Bloque | Estado | PR |
|---|---|---|
| 0: pendientes menores | hecho; queda 0.4, la regla de `_trash/` que David crea en Cloudflare | #10, #12 |
| 1: Fase 2 | pendiente | |
| 2: Fase 3 | pendiente | |
| 3: Fase 4 | hecho; falta la descarga completa en Actions tras el merge | #16 |
| 4: Fase 5 | pendiente | |
| 5: Fase 6 | pendiente | |
| 6: Fase 7 | pendiente | |
