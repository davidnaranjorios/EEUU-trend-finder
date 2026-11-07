# ChatGPT.md — Objetivo, plan aprobado y registro de trabajo

Fecha de referencia: 2026-11-07 (America/Bogota).
Repositorio: davidnaranjorios/EEUU-trend-finder.
Base inspeccionada: main, commit 66ad33a7ae9f7b646ec9103422cc03b3c5ff1f3f.

## 1. Estado y alcance de la aprobación

El propietario aprobó en esta conversación documentar con detalle el objetivo, el trabajo
realizado y el plan aquí descrito. La aprobación inicial autorizó la documentación. Una aprobación posterior autoriza ajustar
este plan e iniciar la implementación del alcance descrito aquí, empezando por Parquet y EDGAR.
No autoriza fusiones, despliegues ni operaciones sobre R2.

Este documento complementa [CLAUDE.md](CLAUDE.md), [docs/plan.md](docs/plan.md) y los
[ADR](docs/decisions/). No reemplaza sus decisiones técnicas ni declara terminadas sus fases.
Las etapas de este documento organizan la validación y la evolución hacia oportunidades
empresariales. Su numeración no equivale a la numeración de fases de CLAUDE.md.

Origen del contexto: conversación «Plan para Trend Finder», identificador
6aede80f-6c98-83e9-a49f-1c4a87c4ae5b, leída mediante la herramienta de chats, y las
aclaraciones y aprobación explícitas del propietario en esta sesión. Los datos de aquella
conversación son antecedentes; deben contrastarse con evidencia actual del repositorio.

## 2. Qué estamos construyendo

Un sistema para descubrir fenómenos económicos estructurales emergentes en Estados Unidos,
comprenderlos con evidencia y convertirlos, después de validarlos, en hipótesis de
oportunidades empresariales originales e investigables.

El propietario es el primer usuario. La prioridad es demostrar utilidad para su investigación
y sus decisiones. No se priorizan monetización, distribución, adquisición de clientes,
arquitectura SaaS ni convertir la herramienta en un producto comercial.

Hay tres niveles distintos:

1. Detectar: identificar cambios persistentes en series y texto, controlando falsos positivos.
2. Comprender: explicar qué cambia, dónde, quién resulta afectado, qué mecanismos podrían
   explicarlo y qué observaciones contradicen la interpretación.
3. Descubrir oportunidades: investigar necesidades, costos, brechas y soluciones posibles,
   incluidas alternativas capaces de transformar la estructura de una industria.

Un score estadístico no mide demanda comercial, rentabilidad ni probabilidad de éxito.
Crecimiento observado tampoco demuestra disposición a pagar.

### Primer MVP validado

Demostrar que al menos un hallazgo permite formular una oportunidad empresarial original,
investigable y respaldada por evidencia, con preguntas abiertas y un experimento concreto.
Tener más señales o más candidatos, por sí solo, no satisface este criterio. Tampoco demuestra
éxito comercial: eso requiere validación posterior.

## 3. Principios y protocolo de trabajo

Se conservan los principios de CLAUDE.md:

- Cero selección de industrias, tendencias o series de interés escrita a mano en producción.
- Aislamiento entre src/ y eval/.
- Control de falsos positivos mediante bootstrap bajo dependencia y BH; BY como comparación.
- Triangulación con fuentes realmente independientes, atendiendo al origen del dato.
- Ausencia de información futura en evaluaciones históricas.
- Evidencia antes que afirmaciones, validación de esquemas y respeto a copyright.
- Ingesta incremental, idempotente y con linaje.

Se añaden los acuerdos de esta sesión:

- Separar hechos observados, inferencias, hipótesis y elementos no verificados.
- No inventar datos, demanda, causalidad ni probabilidades de éxito.
- No descartar automáticamente mercados por concentración, capital necesario o barreras.
- Investigar tanto entrada convencional como soluciones capaces de cambiar esas condiciones.
- Auditar antes de ampliar funcionalidades. No relajar umbrales para producir más candidatos.
- Implementar el diseño aprobado sin pedir aprobación repetida. Si cambia el alcance o diseño,
  presentar la diferencia antes de implementarla. Avisar con PR y evidencia.
- Trabajar en ramas separadas y presentar PR para revisión; no escribir directamente en main.
- No fusionar, desplegar ni modificar datos de producción R2 sin autorización explícita.
- No debilitar pruebas para hacerlas pasar.
- No afirmar validación real basándose exclusivamente en pruebas sintéticas.
- Documentar limitaciones de acceso, datos parciales y cambios de APIs; no ocultarlos.

Cada entrega técnica debe incluir alcance, archivos afectados, pruebas y salida real,
consultas o artefactos de verificación, incertidumbres, enlaces y siguiente decisión requerida.
No es necesario volver a pedir una autorización ya concedida para la misma acción y alcance.

## 4. Arquitectura existente y dirección de evolución

Se conserva la arquitectura modular: ingest/ para fuentes y almacenamiento; detect/ y stats/
para señales y estadística; rank/ para clusters, vínculos, independencia, NAICS y ranking;
report/ para presentación; eval/ para evaluación aislada.

Las futuras capacidades económicas y empresariales se diseñarán separadas del cálculo
estadístico. Como orientación, podrían existir módulos de interpretación, mecanismos,
evidencia, industrias, problemas, mercado, alternativas y evaluación de oportunidades.
Estos nombres son propuestas, no archivos implementados ni decisiones arquitectónicas
aprobadas. Un diseño posterior deberá justificar esquema, entradas, salidas y pruebas.

Las hipótesis de interpretación no deben modificar silenciosamente los datos originales,
los p-values, la FDR o la selección de señales.

## 5. Registro de lo realizado en esta sesión

### Inspección inicial

- Se consultaron metadatos, árbol de archivos, PR, issues y las 30 ejecuciones recientes de
  Actions disponibles en esa consulta.
- Al inicio había 36 PR históricos, todos fusionados, y ningún PR abierto.
- No se encontraron issues independientes de PR en la colección consultada.
- Se leyeron CLAUDE.md, docs/plan.md, ADR 0012 y archivos relevantes de elegibilidad, ranking,
  almacenamiento, esquemas, reporte, HTTP, evaluación y CI.
- No se afirma una auditoría exhaustiva de todos los archivos, ADR o ejecuciones históricas.

### Hallazgos confirmados por código o logs

| Hallazgo | Evidencia | Límite |
| --- | --- | --- |
| Reporte falla al actualizar enlaces históricos | Run 42235624537, job 127952340239: DuckDB no encuentra comove_q durante upsert de series_term_links | No se reprodujo con una copia de R2 |
| Ingesta EDGAR falla descargando contenido | Run 42218050161, job 127892911422: httpx.DecodingError, invalid stored block lengths | No demuestra que todos los documentos o periodos fallen |
| CI de main pasó | Run 42235613190 | No demuestra corrección semántica ni utilidad empresarial |
| Ingesta general pasó | Run 42235613440 | Estado del workflow; no constituye conciliación independiente de cobertura |
| Corrección NAICS S1b está en main | PR #36 y ADR 0012 | Falta comprobar cobertura con datos nuevos y distinguir métodos de anclaje |
| Evaluación histórica aún incompleta en la base inspeccionada | eval/known_events.yaml contiene events: []; no se encontró eval/backtest.py en el árbol | No se ejecutó un backtest |
| El score mide propiedades de señales | src/rank/triangulate.py: fuerza, fuentes, efecto, acuerdo, amplitud y tamaño | No mide viabilidad comercial |
| No se encontraron vetos por concentración o barreras en la lógica inspeccionada | Elegibilidad y ranking revisados | No equivale a medir esas dimensiones |

### Cambio de código preparado

[PR #37](https://github.com/davidnaranjorios/EEUU-trend-finder/pull/37):
«Corrige el reporte al actualizar particiones Parquet de esquemas anteriores».

- Rama: codex/parquet-schema-compatibility.
- Commit: e8b76b21ebde9281fcad47367ea24f03630723b6.
- Archivos: src/ingest/store.py y tests/test_store_evolution.py.
- Antes del upsert, completa campos opcionales ausentes en particiones antiguas con NULL,
  valida las partes y utiliza el intercambio de bucket existente.
- Rechaza columnas obligatorias ausentes. No atribuye validación positiva a vínculos antiguos.
- Añade casos de inserción, reemplazo, conservación histórica, idempotencia y rechazo sin
  modificar archivos cuando falta una columna obligatoria.
- No corrige el error HTTP de EDGAR ni cambia los umbrales o el ranking.
- El PR sigue abierto y sin fusionar al redactar este documento.

Las dos ejecuciones de CI del cambio terminaron en success:

- [CI push 42412138557](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42412138557).
- [CI PR 42412170072](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42412170072).

Se observaron los pasos ruff check y ruff format --check correctos mientras pytest estaba
en curso; posteriormente se confirmó el estado final success de ambas ejecuciones.
No se transcribe un conteo de tests porque no se extrajo la salida final de pytest.
No se ejecutaron pruebas locales: el intento de iniciar la consola falló al crear el proceso.
No se modificaron datos de R2 ni se fusionaron o desplegaron cambios.

El PR se preparó bajo la solicitud inicial de inspeccionar y proponer cambios mediante PR,
antes de incorporar el protocolo de auditoría primero de la conversación adjunta.
Desde la aclaración del propietario, no se continuó ampliando código. Su existencia no
implica aprobación de fusión: debe evaluarse dentro del diagnóstico.

### Antecedentes que requieren reproducción

La conversación adjunta atribuye al Reporte exitoso 42218054855 estas cifras: 334 series con
señal, 505 términos, 119 clusters, 63 vínculos, un candidato triangulado y cobertura NAICS 0 %.
En esta sesión se confirmó que ese workflow terminó en success, pero no se reprodujeron
independientemente esas cifras. No deben presentarse como resultados actuales.
La cobertura puede cambiar tras S1b y la actualización de metadatos.

### Ajustes de rumbo acordados

Primero se propuso una ficha comercial de cinco dimensiones como siguiente mejora.
Después se retiró de la prioridad inmediata para respetar las fases y la auditoría previa.
El plan definitivo aquí aprobado distingue explícitamente interpretación económica,
descubrimiento empresarial, visualización y validación personal.
No se creó AGENTS.md ni se alteraron CLAUDE.md, docs/plan.md o los ADR.

## 6. Plan detallado y criterios de aceptación

No se fijan fechas arbitrarias. Cada etapa avanza por evidencia y criterios acordados.
Las etapas 01–03 preparan la base; las 04–07 desarrollan comprensión, oportunidades y utilidad.
El alcance siguiente está aprobado. Cambios de diseño o alcance requieren nueva aprobación.

### 01. Estabilización inmediata: dos correcciones

Limitar el trabajo inmediato a los fallos que rompen el pipeline; no bloquearlo con una
auditoría completa del repositorio.

- Parquet, PR #37: comprobar que la prueba existente reproduce el caso real del Reporte
  42235624537: series_term_links antigua sin comove_q y upsert nuevo con esa columna.
  Reforzar solo si falta cobertura; no duplicar pruebas. Comprobar conservación, NULL
  para validación desconocida, inserción, reemplazo e idempotencia.
- EDGAR, run 42218050161: reintentos acotados ante httpx.DecodingError. Si un documento
  persiste en fallar, conservar linaje con motivo error_descarga y continuar con los demás.
  Usar el estado existente excluido y su detalle si es compatible; no inventar un dominio.
  No ocultar fallos masivos: registrar intentados, descargados y fallidos, marcar el periodo
  incompleto y mantener posibilidad de reintento. Errores sistémicos de red/acceso deben
  seguir siendo visibles, sin presentar cobertura incompleta como éxito integral.
- Probar una respuesta comprimida defectuosa real en transporte simulado, recuperación,
  agotamiento, linaje, continuación y condición incompleta.
- Un PR por corrección y CI verde, con alcance, archivos, salida real y limitaciones.

Después de revisión y merge aprobado por el propietario: solicitar autorización antes de
re-correr flujos que accedan a R2 o publiquen. Reportar cobertura NAICS real por fuente y
método, denominadores y ambigüedad; CI no sustituye esa verificación.

### 02. Inventario automático y etiquetado humano liviano

Generar por cluster miembros, fuentes publicadoras, origen económico, NAICS y método,
vínculos, estado y regla exacta de exclusión. Identificar la corrida y separar ausencia de
datos de exclusión metodológica. Explicar por qué queda un solo candidato sin bajar umbrales.

Preparar una muestra reproducible de 20 clusters, estratificada por estado y cobertura
(incluir el candidato publicado si existe). Si hay menos de 20, entregar todos y declarar
el total. El propietario etiqueta; el agente no sustituye esa evaluación.
Rúbrica simple: coherencia del fenómeno, pertinencia texto–serie, independencia de origen,
anclaje NAICS, con opciones correcto / dudoso / incorrecto / no evaluable y una justificación.
Conservar ids y evidencia para poder revisar las etiquetas.

Entregable: inventario completo automático, muestra y formulario; sin auditoría manual
exhaustiva. No presentar una muestra como precisión demostrada de todo el sistema.

### 03. Controles ligeros ahora; backtest estricto después

Tras estabilizar y antes de generar fichas: controles negativos y sensibilidad a ventanas
de 3/5/7 años. Definir qué ventana cambia, mantener fecha final, universo comparable,
configuración y semilla documentados; declarar series sin historia suficiente.
Medir falsos positivos, persistencia y estabilidad de señales/ranking sin seleccionar después
la ventana que produzca más candidatos. No cambiar umbrales para mejorar resultados.

El backtest histórico estricto se posterga y su alcance inicial será solo FRED/ALFRED.
No se ha verificado ni implementado reconstrucción temporal equivalente para BLS, BEA,
Census y texto; esto no afirma que esas fuentes carezcan universalmente de vintages.
Conservar evaluación aislada en eval/, eventos y criterios fijados previamente y líneas base.

### 03b. Fuentes de mercado, después de estabilizar

Unidad de análisis: industria NAICS conforme ADR 0012; conservar granularidad, fechas,
método de correspondencia, ambigüedad y cobertura.

Antes de crear adaptadores, comprobar cobertura de los existentes y documentación oficial:

| Fuentes previstas | Pregunta que pueden ayudar a investigar |
| --- | --- |
| Census SUSB, CBP, BDS y Economic Census | Estructura empresarial, tamaño, competencia y entrada |
| BLS PPI y JOLTS; BEA PCE | Precios, presiones de capacidad y contexto de demanda |
| USAspending | Flujos de contratación/gasto público y actores receptores |

Implementar sobre src/ingest/common.py cuando aplique su contrato, con pruebas de esquema
e idempotencia y linaje. Si un dataset exige otro contrato, proponer esa diferencia.
No asumir que conteos de establecimientos prueban concentración de ventas ni que precios,
vacantes, gasto agregado o contratación pública demuestran demanda por una solución.
Documentar población, unidades, frecuencia, restricciones, licencias, revisiones y límites
de correspondencia NAICS. No inventar barreras o dinero a partir de proxies insuficientes.

### 04–05. Una ficha de oportunidad con dos partes

No construir productos separados para interpretación y oportunidad. Una ficha estructurada
por oportunidad enlazada a una o más industrias NAICS, con evidencia trazable.

**Parte A — Comprender:** qué cambió, magnitud/unidades/periodo, mecanismos plausibles,
explicaciones alternativas y evidencia contradictoria. Separar hechos, inferencias e hipótesis.

**Parte B — Oportunidad:** incluir las nueve dimensiones siguientes:

| Dimensión | Contenido requerido |
| --- | --- |
| Fenómeno | Cambio observado y vínculo con la Parte A |
| Evidencia | Fuentes, fechas, calidad, procedencia y contradicciones |
| Demanda | Necesidad, usuario, comprador y disposición a pagar; distinguir proxies |
| Brecha | Problema, costo para el afectado y limitaciones de soluciones actuales |
| Competencia | Actores, sustitutos y concentración cuando sea medible |
| Dinero | Quién paga, por qué, costos e ingresos hipotéticos y evidencia disponible |
| Entrada | Capital, capacidades, regulación y rutas de entrada |
| Riesgo | Supuestos críticos, restricciones y explicaciones adversas |
| Confianza | Respaldo y dudas por afirmación, sin probabilidad ficticia |

Por fila y, si contiene afirmaciones distintas, por afirmación: ✅ demostrado /
🟡 indicio / ⚪ hipótesis / ❔ desconocido, con evidencia y limitación.
Un hecho de crecimiento no convierte la demanda comercial en demostrada.
Nunca un score único de oportunidad; preservar el score estadístico como dato separado.

Añadir soluciones convencionales y radicales, condiciones que refutarían cada hipótesis y
un experimento concreto con criterio de éxito o rechazo fijado antes de ejecutarlo.
Investigar qué problema revela cada fenómeno, quién soporta sus costos, soluciones existentes
y alternativas que podrían eliminar procesos o transformar la estructura económica.

Concentración o capital no descartan: se clasifican. Interesa especialmente
«industria concentrada + entrada barata o vía disruptiva», sin presuponer que esa combinación
existe ni que una tecnología propuesta es viable.

### 06. Resumen sin LLM y fichas completas a demanda

1. Resumen semanal de todas las oportunidades identificadas, calculado sin LLM y con
   reglas y evidencia deterministas; datos faltantes explícitos.
2. Fichas completas solo para las oportunidades que seleccione el propietario.
3. Navegar oportunidad → fenómeno → series/texto → fuentes/fechas y
   oportunidad → supuestos → contradicciones → experimentos → resultados.
4. Respetar copyright y no ocultar incertidumbre en las comparaciones.

La redacción de la ficha se generará vía OpenRouter con el modelo elegido por el propietario
en config.yaml. Solo recibirá datos de la ficha; no completará ausencias con conocimiento
no documentado. Credenciales fuera de config.yaml, límites de costo y tratamiento visible
de errores; ninguna llamada pagada en pruebas normales.

Cada afirmación generada debe remitir a identificadores de evidencia. Validar cifras,
unidades, periodo, población, fuente y atribución: una cifra presente pero aplicada al
concepto equivocado también es un error. Rechazar o señalar texto sin respaldo, no publicarlo
como demostrado. Probar con respuestas simuladas, incluidas cifras inventadas y atribuciones
incorrectas. La revisión humana sigue siendo necesaria para conclusiones económicas.

### 07. Validación personal y mejora continua

El propietario utilizará los hallazgos para investigar oportunidades y registrará:

- Qué hallazgo motivó una investigación y qué información nueva aportó.
- Qué hipótesis tenía, qué esperaba observar y qué prueba realizó.
- Qué decidió: investigar, experimentar, posponer o descartar, con sus motivos.
- Qué hipótesis fueron refutadas, útiles o siguen abiertas.
- Qué problemas u oportunidades conoció por otras vías y el sistema pasó por alto.
- Tiempo y esfuerzo de investigación, y resultados posteriores cuando estén disponibles.

Separar calidad de detección, interpretación, oportunidad y decisión. No atribuir causalmente
una mejora al sistema sin un diseño de comparación apropiado.
Establecer una referencia de investigación convencional y criterios de utilidad antes del piloto.
Registrar también resultados negativos para no aprender solo de historias exitosas.

Entregable: historial de decisiones y revisión periódica de utilidad, errores y omisiones.
Aceptación: poder evaluar si la herramienta mejora el proceso personal de descubrimiento,
con ejemplos respaldados y límites claros; no solo contar visitas o candidatos.
Las mejoras derivadas del uso deberán pasar por propuesta, aprobación y pruebas.

## 7. Automatización técnica: seguimiento independiente

La Fase 7 de CLAUDE.md es automatización, no la etapa 07 de validación personal de este plan.

Tras estabilizar y validar los componentes, verificar una ejecución semanal programada que
actualice fuentes, detectores, ranking y reporte, deje artefactos y logs y termine sin
intervención. Registrar duración, reanudación, fallos y cobertura.
La automatización no sustituye evaluación histórica, coherencia económica ni utilidad personal.
Despliegues y cambios en datos remotos requieren autorización dentro del alcance acordado.

## 8. Orden operativo aprobado y autorizaciones

1. Verificar PR #37 y preparar corrección EDGAR en PR independiente.
2. Con autorización del propietario para R2/despliegue, re-correr pipeline y medir NAICS.
3. Inventario automático de exclusiones y muestra de 20 para etiquetado del propietario.
4. Controles negativos y ventanas 3/5/7 antes de fichas; incorporar fuentes de mercado.
5. Una ficha de dos partes y resumen semanal sin LLM; redacción completa a demanda.
6. Registro personal simple.
7. Backtest estricto FRED/ALFRED y automatización integral.

Registro personal mínimo: fecha, oportunidad, qué investigué, evidencia, qué decidí,
qué se refutó y siguiente paso; puede registrar omisiones del sistema sin construir
un producto adicional.

Avisar de cambios ya aprobados con PR, tests y salida real, run de Actions y limitaciones.
No pedir aprobación repetida si el diseño permanece dentro del alcance.
El propietario aprueba los merges. Pedir autorización antes de tocar R2 o desplegar.
Cambios de principios o diseño, criterios del backtest y acceso a producción conservan
sus puntos específicos de aprobación.

## 9. Registro de avance y evidencias futuras

| Elemento | Estado al 2026-11-07 | Siguiente evidencia necesaria |
| --- | --- | --- |
| Plan y documentación | Aprobados para documentar en esta sesión | Revisión del PR documental |
| Estabilización inmediata | En curso, alcance acotado aprobado | Parquet y EDGAR, pruebas y CI |
| Compatibilidad de esquema | PR #37 abierto; fallo comove_q reproducido contra main, pruebas correctas con arreglo | Revisión y ejecución real autorizada |
| Error HTTP de EDGAR | PR #39 preparado, pruebas locales correctas | CI completa y revisión |
| Coherencia y candidato único | Pendiente de explicación | Inventario, muestra auditada y razones de exclusión |
| NAICS tras S1b | Pendiente de validación real | Cobertura por fuente y método |
| Backtest | Pendiente | Eventos, umbrales previos, datos temporales y reporte |
| Interpretación y oportunidades | Planificadas | Diseños aprobados y evidencia validada |
| Tablero de oportunidades | Planificado | Diseño, trazabilidad y prueba de uso |
| Validación personal | Planificada | Registro y criterios del piloto |
| Automatización completa | No verificada en esta sesión | Corrida programada integral |

Actualizar este registro con fecha, commit/run, pruebas, consultas, resultado y limitaciones.
No borrar antecedentes relevantes ni convertir «implementado» en «validado» sin evidencia.

## 10. Enlaces de referencia

- [CLAUDE.md](CLAUDE.md), [plan previo](docs/plan.md), [ADR 0012](docs/decisions/0012-anclaje-naics.md).
- [PR #36, NAICS S1b](https://github.com/davidnaranjorios/EEUU-trend-finder/pull/36).
- [PR #37, compatibilidad Parquet](https://github.com/davidnaranjorios/EEUU-trend-finder/pull/37).
- [Reporte fallido](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42235624537).
- [Ingesta texto fallida](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42218050161).
- [Reporte histórico exitoso](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42218054855).
- [CI de main](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42235613190).
- [Ingesta general](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42235613440).

## 11. Revisión aprobada del plan

El propietario aprobó incorporar el feedback de Claude con las precisiones del agente e
iniciar el punto 1. Esta revisión sustituye la auditoría exhaustiva inicial por estabilización,
reduce el etiquetado a 20 clusters a cargo del propietario, divide la validación temporal,
incorpora fuentes de mercado y unifica la ficha con redacción OpenRouter a demanda.
No se cambian umbrales ni se autoriza automáticamente merge, R2 o despliegue.

## 12. Ejecución del punto 1 autorizado (2026-11-07)

### Parquet: verificación del fallo observado

Se ejecutó la prueba existente de series_term_links antiguas sin campos de co-movimiento
contra store.py de main y se obtuvo BinderException por comove_q, igual que en el run
42235624537. Con el almacén del PR #37, la prueba y las regresiones de almacenamiento pasan:

```text
BASELINE: reproduced BinderException: comove_q missing in historical series_term_links
pytest tests/test_store_evolution.py tests/test_storage_export.py -q
14 passed in 12.52s
```

No se duplicó la prueba ni se modificó código adicional en ese PR. Reproducción del esquema
con datos sintéticos, no reproducción sobre los datos de R2. La evidencia está en la descripción
actualizada del PR #37; ambas CI existentes siguen siendo la referencia de ese commit.

### EDGAR: corrección independiente

[PR #39](https://github.com/davidnaranjorios/EEUU-trend-finder/pull/39), rama
codex/edgar-decoding-retry, commit 4be45feaf6280cd40de33d92d483b92d18820dcd.

Archivos: src/ingest/http.py, src/ingest/edgar.py, src/ingest/text.py y tests/test_text_ingest.py.

- Captura httpx.DecodingError con los reintentos acotados y rate limiter existentes.
- Al agotarse, ApiDecodingError permite registrar el documento como excluido con
  detail=error_descarga=DecodingError y continuar con los siguientes.
- Expone download_attempted, download_succeeded y download_failed; un agotamiento de
  descompresión marca el periodo incompleto, no cerrado, y lo deja elegible para reproceso.
- La CLI devuelve 11 si hubo periodos incompletos, incluso si terminó de recorrer la lista.
  run_texto.sh sincroniza antes de propagar ese código y no lo confunde con el 10 de más tramos.
- Los 404 mantienen el tratamiento terminal anterior; red, acceso, índice o metadatos globales
  siguen pudiendo abortar. No se omiten errores globales como si fueran documentos individuales.
- El estado incompleto conserva resultados parciales explícitos y linaje; no equivale a un
  universo completo apto para afirmar cobertura total. Un workflow independiente debe consultar
  ese estado antes de usar datos parciales.

Verificación local:

```text
ruff check: All checks passed!
ruff format --check: 114 files already formatted
git diff --check: sin errores
pytest -k 'corrupt_http or all_document_decoding or incomplete_cli':
4 passed, 16 deselected in 12.79s
pytest tests/test_text_ingest.py tests/test_fred.py -k 'not parallel_workers' -q
60 passed, 1 deselected in 163.45s
```

Pruebas con respuesta gzip defectuosa real en MockTransport: recuperación, agotamiento,
linaje, continuación, todos los documentos fallidos, reproceso saludable y salida CLI no exitosa.
Las primeras ejecuciones Windows fallaron por ausencia de tzdata y por crear forkserver
incluso con workers=0. Se instaló tzdata solo en el entorno local y se difirió la creación
del contexto al modo paralelo. No se modificaron dependencias del repositorio.
La prueba paralela se excluyó únicamente en Windows; la CI de Ubuntu ejecuta toda la suite.

CI EDGAR: [push 42415782626](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42415782626)
y [PR 42415812090](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42415812090).
CI push terminada en success; salida real del job 128533440206:

```text
470 passed in 402.92s (0:06:42)
```

La ejecución pull_request 42415812090 también terminó en success. La CI completa
incluye la prueba paralela excluida solo de la ejecución local Windows.

Referencias consultadas: [excepciones HTTPX](https://www.python-httpx.org/exceptions/) y
[acceso SEC](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).
Se mantuvieron límites y User-Agent; no se realizaron solicitudes de prueba a SEC.

No se modificó R2, no se desplegó ni se fusionó ningún PR. El punto 2 requiere autorización
para la ejecución real que use R2 o publique; las pruebas locales y CI no la sustituyen.

## 13. Plan operativo por fases y delegación autorizada

El propietario autorizó planear acciones por fases, desplegar agentes e iniciar trabajo.
Se asignaron tres tareas independientes: inventario local, diseño de validación ligera y
revisión de fuentes de mercado. Los agentes no reciben permiso implícito de merge, R2 o despliegue.

Posteriormente el propietario autorizó explícitamente fusionar #37 y #39. Ambos se fusionaron
tras comprobar head SHA y CI verde. Commits de merge: #37 4a859ecfdbf3604a94c2a8394623eb5a9977fff6;
#39 554eb62c017b6bf33594f159017896773a02d42d. Main queda en este último commit al registrar
la sección. Son correcciones integradas; la verificación real de datos continúa pendiente.
No se fusionó el PR documental #38. Los estados anteriores del registro son históricos.

| Fase operativa | Acción concreta | Evidencia/condición de salida | Estado |
| --- | --- | --- | --- |
| 0. Integrar estabilización | Merge #37 y #39 aprobado por propietario | PR fusionados y CI de main posterior | Fusionados; CI posterior en curso |
| 1. Verificación real acotada | Reporte en main con publish=false | Estado del run, tablas escritas, cobertura NAICS por fuente/método, exclusiones y limitaciones | Se pidió autorización específica R2; no ejecutado todavía |
| 2. Inventario liviano | Herramienta local de solo lectura, manifiesto y muestra 20 | Tests, inventario por cluster y rúbrica sin etiquetas del agente | Implementación delegada en rama aislada |
| 3. Controles ligeros | Universo común, corte fijo, 3+3/5+5/7+7 años de slope | Diseño reproducible y luego métricas/controles; no afirmar backtest | Diseño preparado; implementación posterior acotada |
| 4. Fuentes de mercado | Comprobar cobertura antes de ampliar adaptadores | Fixtures oficiales, esquema/idempotencia/NAICS y límites | Investigación de código y fuentes oficiales realizada |
| 5. Ficha y resumen | Una ficha A/B y resumen sin LLM | Nueve dimensiones, evidencia y experimentos; OpenRouter bajo demanda | Planificado, sin implementación |
| 6. Registro personal | Tabla simple para propietario | Investigación, decisión, refutación y siguiente acción | Planificado |
| 7. Evaluación histórica/automatización | FRED/ALFRED primero y corrida integral | Criterios previos, reproducibilidad, autorización de operaciones | Posterior |

La fase 1 propuesta usa los datos actualmente almacenados y no demuestra reingesta completa.
Cualquier ejecución de Ingesta, Ingesta texto, Detectores o Reporte que lea/escriba R2 requiere
su autorización concreta. La lectura de artefactos de GitHub no equivale a acceso a R2.
El artefacto público del Reporte histórico solo contiene el sitio; no permite reconstruir
por sí solo el inventario privado completo de clusters/exclusiones. No inventar esas filas.

### Diseño de validación ligera: alcance preciso

- slope_window de 3/5/7 significa comparar 3+3, 5+5 y 7+7 años. No es la longitud total.
- Preparar una sola vez con W7 y congelar Item.growth, escala, persistencia, grilla, universo,
  fecha final y configuración. Después variar solo FamilyParams.window en memoria.
- W7 exige al menos 168 crecimientos mensuales, 61 trimestrales y 40 anuales con las reglas
  inspeccionadas. Declarar cobertura perdida; no reducir mínimos ni cambiar YAML productivo.
- context.windows_years ya es descriptivo; no cuenta como validación del detector.
- regime no usa slope_window: ejecutarlo como referencia fija no mide sensibilidad 3/5/7.
- Reunir todas las fuentes/grillas antes de BH por detector y escenario; BY solo comparación.
  La unión de señales de los tres escenarios no es una nueva familia controlada al mismo q.
- Misma semilla no garantiza mismos bloques cuando cambia la forma de remuestreo. Documentar
  hashes, librerías, parámetros y semilla, y recortar niveles antes de preparar.
- Métricas: rechazos BH/BY, retención, Jaccard, signo y cambios de efecto/p/q. Si ambos conjuntos
  están vacíos, marcar no evaluable: no afirmar estabilidad perfecta.
- Esto mide señales sobre snapshot vigente, no estabilidad del ranking completo ni anticipación.
  Un ranking requiere también clustering, texto y correspondencia por miembros.
- Reutilizar controles iid/AR1/factor y salto aislado ya existentes; una campaña breve verifica
  cableado, no certifica FDR. Incluir control positivo para no celebrar falta de potencia.
- Antes de una campaña: fijar escenarios, réplicas, semillas, límites de cómputo y métricas.
  El backtest estricto posterior queda FRED/ALFRED; las demás reconstrucciones no están verificadas.

### Fuentes de mercado: diagnóstico previo a implementación

El código inspeccionado puede descubrir familias genéricas sin demostrar cobertura real de
producción. No crear adaptadores duplicados antes de probar los actuales.

| Fuente | Brecha y siguiente comprobación | Límite económico |
| --- | --- | --- |
| BDS | Fixture timeseries con YEAR/time, filtros y dimensiones del adaptador Census | Aperturas/cierres no miden costo de entrada |
| CBP | Releases anuales no entran en el filtro actual c_isTimeseries; diseñar ensamblado por años | Establecimientos no equivalen a firmas/cuotas de ventas |
| SUSB | CSV/XLS requieren descubrimiento y normalización propios sobre contrato común | Estructura empresarial no prueba entrada barata |
| Economic Census | Releases quinquenales como contexto fechado; investigar concentración oficial ecnsize | Mercado de una solución no equivale a toda la industria |
| PPI | Comprobar familias pc/wp en BLS existente | Índices de precios no son precios absolutos ni márgenes; mercancía no es NAICS |
| JOLTS | Comprobar jt y agregación industrial | Vacantes laborales no demuestran demanda de clientes |
| PCE | Verificar tablas BEA existentes y puente producto–industria | Gasto nominal no prueba volumen ni disposición a pagar |
| USAspending | API POST y datos de adjudicación requieren contrato/esquema apropiados | Separar obligaciones/desembolsos/techos; no sumarlos ni llamarlos mercado privado |

Primero fixtures pequeños, documentados y sin red para BDS/JOLTS/PPI. Corregir solo brechas
estructurales demostradas. Después CBP y concentración oficial; SUSB/USAspending requieren
resolver contrato y trazabilidad. Preservar versión NAICS, supresión, firma/establecimiento,
universo empleador, geografía y puentes muchos-a-muchos. No convertir supresión en cero,
no sumar jerarquías superpuestas ni contar productos del mismo origen como fuentes independientes.

Referencias oficiales consultadas por el agente:
[BDS](https://www.census.gov/programs-surveys/bds/data.API.html),
[CBP](https://www.census.gov/data/developers/data-sets/cbp-zbp/cbp-api.html),
[SUSB](https://www.census.gov/programs-surveys/susb/data.html),
[Economic Census](https://www.census.gov/data/developers/data-sets/economic-census.html),
[concentración](https://www.census.gov/newsroom/press-releases/2025/establishment-and-firm-size-statistics.html),
[PPI](https://www.bls.gov/opub/hom/ppi/concepts.htm),
[JOLTS NAICS](https://www.bls.gov/jlt/jltnaics.htm),
[PCE](https://www.bea.gov/data/consumer-spending/main),
[USAspending](https://api.usaspending.gov/docs/endpoints).
No se ejecutaron descargas de mercado ni se verificó catálogo de producción en esta investigación.

## 14. Reporte real verificado tras las correcciones

El propietario autorizó ejecutar Reporte en main con publish=false y posteriormente informó
que lo había completado. Se verificó el run
[42422615842](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42422615842),
commit 554eb62c017b6bf33594f159017896773a02d42d, resultado success.

Ranking, construcción, verificación de copyright y sincronización R2 terminaron correctamente.
Publicar en Cloudflare Pages y Preparar GitHub Pages quedaron skipped; el job pages también
quedó skipped. El sitio solo se conservó en el artefacto sitio-42422615842 (id 13593604933).
No se ejecutó un despliegue.

### Salida real de ranking

- rank_run_id: f91893071f6041c3a4d980d659c697ab.
- numeric_run_id: c1529146f3e14c89b64b8083c6c7be6e.
- Texto EDGAR: b6fa78e1ce5148a2b60e14093515d832.
- Texto Federal Register: e76b68eac214400cbe2436c3d3406f23.
- 334 series con señal, 505 términos con señal, 119 clusters (58 con varios miembros).
- 63 vínculos, 1 candidato, 118 con estado una_fuente, 0 fuera del tope y 1 publicable.
  Publicable en las tablas no significa desplegado: la publicación estuvo desactivada.

### Cobertura NAICS observada

| Fuente | Objetos evaluados | Con NAICS | Porcentaje registrado |
| --- | ---: | ---: | ---: |
| BEA | 12 | 0 | 0 % |
| BLS | 168 | 25 | 14,9 % |
| Census | 8 | 0 | 0 % |
| EDGAR | 306 | 0 | 0 % |
| FRED | 146 | 25 | 17,1 % |

Estos denominadores son los objetos evaluados para el anclaje en esa corrida, no todos los
catálogos ni todas las industrias estadounidenses. Federal Register queda fuera del denominador
según el diseño. El log registra cobertura por fuente, pero no desglosa por método.
No se atribuye el resultado a codigo_en_metadatos, nombre_oficial o herencia sin leer las
filas naics_links de la misma corrida. Continúa pendiente ese desglose.

La cobertura ya no es cero para todas las fuentes, pero sigue siendo baja y no demuestra
cumplimiento del objetivo de cobertura ni corrección semántica de los enlaces.
No se concluye la causa de los ceros sin revisar metadatos, esquema y linaje de cada fuente.

### Verificación de publicación y almacenamiento

La comprobación independiente de copyright recorrió 140 archivos y reportó leaks=0,
con 74751 ids prohibidos y 49517 títulos prohibidos. El sitio construido tiene 140 archivos,
66551453 bytes y 376084 fichas de explorador. No son métricas de precisión económica.

Los logs de rclone reportan 0 differences found y 40 matching files en processed/rank,
16 en caché de embeddings, 12 en reports y 4 en logs/reporte; sync_r2 terminó ok.
Son resultados del workflow, no una segunda comparación independiente descargada localmente.

### Qué quedó validado y qué no

La corrección Parquet superó el caso real del reporte que antes abortaba por comove_q.
Los dos merges también tienen CI de main en verde (runs 42421995597 y 42421999294).
Esta ejecución no reingestó EDGAR, BLS u otras fuentes ni volvió a correr los detectores.
Por tanto, no demuestra recuperación real de la ingesta EDGAR ni cobertura posterior a una
reingesta completa. Las pruebas sintéticas/CI de EDGAR y esta verificación de Reporte son
complementarias, no intercambiables.

El siguiente paso de diagnóstico es el inventario de exclusiones con orígenes y NAICS por
método, seguido de la muestra de hasta 20 para el propietario. No se cambian umbrales.
La herramienta local tiene pruebas; todavía no se ha ejecutado sobre las tablas reales.

## 15. Inventario preparado para revisión

Se abrió [PR #40](https://github.com/davidnaranjorios/EEUU-trend-finder/pull/40), rama
codex/cluster-audit, commit 1f083c832597b1b7d34ae8d6e9ffeedb06b0815e, sobre main con ambas
correcciones de estabilización incorporadas.

Archivos: eval/cluster_audit.py, tests/test_cluster_audit.py, docs/cluster-audit.md.
La herramienta lee tablas Parquet locales de una corrida explícita y genera inventario.json,
muestra.json, etiquetas.csv, rubrica.md y manifest.json. No consulta red/R2 ni modifica datos.
La metadata local opcional añade títulos y conserva copyright; ausencias permanecen desconocidas.

El manifiesto conserva cobertura registrada en rank_runs y calcula objetos únicos de toda
naics_links por fuente/método, no solo miembros de clusters. No suma métodos superpuestos
ni divide conteos de un universo por denominadores de otro. Si falta tabla, el conteo es
unknown; una tabla disponible sin filas puede contener cero. Los umbrales proceden de los
parámetros guardados de la corrida, no del YAML actual.

Pruebas locales del agente, revisadas junto con el código por el agente principal:

```text
ruff check .: All checks passed!
ruff format --check .: 117 files already formatted
pytest tests/test_cluster_audit.py tests/test_isolation.py tests/test_no_hardcoded_topics.py -q
38 passed in 10.23s
git diff --check: correcto
```

La carga de pruebas no importa eval como dependencia de src. Se comprueban legado, tablas
opcionales, reglas persistidas, muestreo hasta20 reproducible, datos faltantes, metadata,
entradas/salidas separadas y denominadores/métodos NAICS sin dobles conteos.
La CI del PR [42423108202](https://github.com/davidnaranjorios/EEUU-trend-finder/actions/runs/42423108202)
terminó en success; salida real: 482 passed in 381.84s (0:06:21). Lint y formato también
correctos. El PR queda listo para revisión, sin fusionar.

No se fusionó #40 ni se generó todavía la muestra de 20 reales. La autorización del Reporte
no incluye por sí sola extraer archivos de R2 al equipo o publicar material de auditoría.
El siguiente paso requiere una copia local autorizada de processed/rank y series_meta para
la corrida f91893071f6041c3a4d980d659c697ab, conservando la salida como material interno.
