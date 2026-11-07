# ChatGPT.md — Objetivo, plan aprobado y registro de trabajo

Fecha de referencia: 2026-11-07 (America/Bogota).
Repositorio: davidnaranjorios/EEUU-trend-finder.
Base inspeccionada: main, commit 66ad33a7ae9f7b646ec9103422cc03b3c5ff1f3f.

## 1. Estado y alcance de la aprobación

El propietario aprobó en esta conversación documentar con detalle el objetivo, el trabajo
realizado y el plan aquí descrito. La aprobación autoriza esta documentación; no autoriza
automáticamente implementar todas las etapas, fusionar PR, desplegar ni modificar datos de R2.

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
- Proponer el diseño y obtener aprobación antes de iniciar una nueva implementación.
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
Antes de implementar cada bloque se presentará su diseño para aprobación.

### 01. Auditoría inicial, estabilización e integridad

Primero completar el diagnóstico de solo lectura y las pruebas locales seguras:

1. Leer los ADR restantes relevantes y conciliar documentación con código y ejecuciones.
2. Construir una tabla de problemas confirmados, sospechas y aspectos no verificables.
3. Revisar alcance, riesgos y pruebas del PR #37, incluida compatibilidad de particiones
   mixtas, conservación de registros, interrupciones e idempotencia.
4. Analizar el manejo de errores HTTP de EDGAR y diseñar una prueba de respuesta comprimida
   defectuosa, reintentos acotados y continuidad sin pérdida silenciosa.
5. Identificar datos y accesos necesarios para reproducir una corrida sin tocar producción.
6. Presentar correcciones por separado y obtener aprobación antes de implementarlas.

Tras aprobar correcciones: verificar ingesta → detección → ranking → reporte con un manifiesto
de versiones de código, configuración, datos, periodos y ejecución. Conciliar conteos de
entrada y salida, exclusiones y artefactos; verificar linaje, copyright e idempotencia.

Entregable: diagnóstico y plan de corrección por dependencia y riesgo; posteriormente,
corrida completa reproducible con logs y conteos conciliados.
Aceptación: explicar discrepancias y comprobar que repetir la ejecución no duplica ni pierde
filas. CI verde es necesaria, pero no suficiente.
Dependencia: aprobación explícita antes de operaciones sobre R2 o despliegues.

### 02. Auditoría semántica, independencia y anclaje industrial

1. Recuperar los clusters de una corrida identificada; si se usa la corrida histórica, verificar
   primero los 119 registros atribuidos a ella.
2. Crear inventario completo con miembros, señales, fechas, vínculos, fuentes, origen,
   clasificación y motivo de exclusión.
3. Revisar una muestra estratificada de clusters y vínculos y el candidato publicado.
   Acordar el muestreo y la rúbrica antes de etiquetar; ampliar revisión según los errores.
4. Medir coherencia del fenómeno, falsas asociaciones y duplicados. La similitud de títulos
   por sí sola no prueba relación económica ni independencia.
5. Descomponer por qué queda un solo candidato: evidencia disponible, cobertura de texto,
   validación de vínculos, origen compartido, copyright y demás reglas realmente aplicadas.
6. Validar NAICS después de actualizar metadatos y reprocesar lo necesario. Separar códigos
   explícitos, nombres oficiales, herencia y concordancias; declarar ambigüedad y cobertura
   por fuente con denominador, exclusiones y método.

Entregable: tabla auditable, rúbrica, etiquetas, errores y cobertura industrial.
Aceptación: causa de exclusión trazable, métricas de coherencia y errores, fuentes derivadas
no contadas dos veces. Umbrales de calidad se acuerdan antes de medir.
No bajar umbrales estadísticos o semánticos para inflar el número de candidatos.

### 03. Validación estadística y temporal

Corresponde a revisar la Fase 2 y desarrollar la Fase 6 de CLAUDE.md:

- Auditar calibración de p-values, FDR bajo dependencia, potencia, persistencia, piso de p,
  sensibilidad a ventanas y estabilidad del ranking.
- Combinar simulaciones, controles negativos y eventos históricos; reportar sus límites.
- Proponer eventos con fechas y referencias en eval/ y aprobarlos antes de la evaluación.
- Fijar de antemano umbrales, métricas, ventanas y líneas base. No ajustar criterios después
  de ver el resultado para declararlo exitoso.
- Reconstruir información disponible en cada fecha. ALFRED no resuelve por sí solo el
  historial de BLS, BEA, Census y texto: documentar publicación, revisiones y cobertura.
- Si una fuente no permite reconstrucción fiable, declarar la limitación y separar ese
  experimento de un backtest estricto.
- Medir recuperación, anticipación, volumen de candidatos y comparación con líneas base.
  No llamar «precisión» al recall de un conjunto incompleto de eventos conocidos.

Entregable: reporte reproducible con manifiesto de datos, métricas y limitaciones.
Aceptación: ausencia de fuga temporal, aislamiento de eval/, comparación con criterios previos
y resultados reales. Un resultado inferior al objetivo se informa como tal.

### 04. Interpretación económica

Convertir fenómenos validados en fichas explicativas:

- Qué cambió, magnitud, unidades, horizonte, persistencia y revisiones.
- Industrias y geografía cuando existan datos adecuados; método y confianza del anclaje.
- Actores afectados, precios, cantidades, costos, capacidad y productividad cuando sean medibles.
- Mecanismos plausibles y explicaciones alternativas, sin convertir correlación en causalidad.
- Relaciones entre indicadores, evidencia contradictoria y datos aún necesarios.
- Concentración y barreras como contexto económico; límites de cobertura de cada medida.

Cada afirmación tendrá evidencia, fecha, fuente y etiqueta: hecho, inferencia o hipótesis.
La falta de datos se mostrará como desconocida, no como cero o ausencia del fenómeno.

Entregable: ficha económica trazable y revisión de interpretaciones alternativas.
Aceptación: ningún enunciado causal o de demanda sin soporte suficiente; incertidumbre visible.
Esta etapa explica el fenómeno; la siguiente investiga qué podría hacerse frente a él.

### 05. Motor de descubrimiento empresarial

Para cada fenómeno suficientemente validado, investigar:

1. Qué problemas provoca o revela y cómo podrían evolucionar.
2. Quién los sufre, quién soporta sus costos y quién podría decidir o pagar una solución.
3. Qué evidencia mide la frecuencia, severidad y costo del problema.
4. Qué soluciones, proveedores, sustitutos y prácticas internas existen.
5. Dónde presentan limitaciones: desempeño, costo, acceso, tiempo, regulación u otras
   restricciones sustentadas en evidencia.
6. Qué soluciones convencionales podrían mejorar la situación.
7. Qué alternativas radicalmente distintas podrían eliminar procesos, sustituir tecnologías
   o cambiar la estructura de mercado.
8. Qué capacidades, capital, restricciones físicas y regulatorias necesita cada alternativa.
9. Qué contradice la hipótesis, qué falta saber y qué experimento podría refutarla.

Crecimiento, concentración, barreras, precios y demanda serán dimensiones separadas.
La concentración no supone automáticamente inviabilidad; las barreras pueden motivar una
hipótesis transformadora, pero no garantizan que sea técnicamente posible.
Distinguir necesidad observada, interés declarado, disposición a pagar y demanda comprobada.

Entregable: hipótesis de oportunidades con evidencia favorable, contraargumentos, alternativas,
preguntas pendientes, supuestos críticos y siguiente experimento.
Aceptación: problema y actor concretos, trazabilidad al fenómeno y condiciones de refutación.
No producir probabilidades de éxito sin una base empírica y un método de calibración.
El ranking de oportunidades será distinto del ranking estadístico; cualquier priorización
deberá justificar criterios y mostrar incertidumbre y sensibilidad.

### 06. Evaluación y visualización para decisiones

Diseñar un tablero que permita navegar:

Oportunidad → problema → fenómeno → señales/series/texto → fuentes y fechas,
y también desde oportunidad → supuestos → contradicciones → experimentos → resultados.

Las fichas comparables incluirán:

- Problema, actores, soluciones existentes y alternativas propuestas.
- Evidencia de crecimiento, concentración, barreras, precios y demanda; unidades y periodo.
- Procedencia, fecha de consulta y disponibilidad histórica cuando corresponda.
- Supuestos, contraargumentos, datos faltantes e incertidumbre por dimensión.
- Experimento recomendado, criterio previo de éxito o rechazo y resultado cuando exista.
- Diferenciación visible entre señal estadística, interpretación y validación comercial.

Evitar un único score que oculte compensaciones o convierta datos faltantes en malos resultados.
El usuario debe poder abrir la evidencia que sostiene cada afirmación y comparar hipótesis
con distinta cobertura sin atribuirles certezas equivalentes.
El contenido publicado conservará la barrera de copyright.

Entregable: diseño aprobado y, después, tablero con fichas comparables.
Aceptación: recorridos verificables hasta la evidencia, incertidumbre visible y ninguna
confusión entre crecimiento y demanda comprobada. Validar utilidad de navegación con el
propietario antes de ampliar la interfaz.

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

## 8. Próximo trabajo y puntos de aprobación

El siguiente trabajo propuesto es completar la auditoría de la etapa 01 y preparar la tabla de
evidencia para la etapa 02. Prioridades de investigación:

1. Explicar la reducción de señales/clusters a un candidato triangulado.
2. Verificar el estado real del anclaje NAICS tras S1b.
3. Evaluar el PR #37 y el diseño de corrección HTTP de EDGAR.
4. Identificar qué datos faltan para reproducir resultados de forma segura.

Este documento no inicia por sí solo esas implementaciones.
Puntos de aprobación: diseño de cada bloque; eventos y criterios previos del backtest;
cambios de principios o arquitectura; operaciones en producción; fusión y publicación.

## 9. Registro de avance y evidencias futuras

| Elemento | Estado al 2026-11-07 | Siguiente evidencia necesaria |
| --- | --- | --- |
| Plan y documentación | Aprobados para documentar en esta sesión | Revisión del PR documental |
| Auditoría completa | Parcial | ADR restantes, artefactos y tabla de diagnóstico |
| Compatibilidad de esquema | PR #37 abierto, dos CI exitosas | Revisión y reproducción con datos representativos |
| Error HTTP de EDGAR | Diagnosticado, sin corrección | Diseño y prueba de regresión |
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
