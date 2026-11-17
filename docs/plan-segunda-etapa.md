# Plan: segunda etapa de Trend Finder

Aprobado por David el 2026-11-17. Complementa (no reemplaza) `docs/rediseno-oportunidades.md`:
las 9 dimensiones de la ficha pasan a ser el **respaldo de datos** de cuatro preguntas.

## Objetivo

Para cada fenómeno que pasa el detector estadístico, entregar un **insumo de búsqueda** que
responda cuatro preguntas:

1. ¿Qué cambió realmente?
2. ¿Qué consecuencias genera?
3. ¿Dónde puede capturarse valor?
4. ¿Qué evidencia falta?

La salida no es una conclusión de negocio: es un punto de partida con datos y con preguntas
listas para que una IA de investigación (Elicit, AnswerThis) las investigue. El trabajo de campo
(entrevistas, pruebas de mercado) queda fuera del sistema.

## Estructura en tres capas

**Capa 1 — Detección (existe).** Detector estadístico con control de falsos positivos, origen
independiente, trayectoria y marcas. Decide qué fenómenos pasan. No se cambia.

**Capa 2 — Antecedentes: las 9 dimensiones (datos, sin IA).** Fenómeno, Evidencia, Demanda,
Brecha, Competencia, Dinero, Entrada, Riesgo, Confianza. Cada una es un conjunto de cifras con
fuente y fecha, calculadas por industria; no redactan conclusiones.

**Capa 3 — Síntesis: las 4 preguntas.**

| Pregunta | Dimensiones que usa | Cómo se responde |
|---|---|---|
| 1. ¿Qué cambió realmente? | Fenómeno, Evidencia, Demanda, Brecha | Reglas fijas, sin IA |
| 2. ¿Qué consecuencias genera? | Brecha, Competencia, Riesgo | Cifras con reglas fijas; narración con IA bajo pedido |
| 3. ¿Dónde puede capturarse valor? | Demanda, Dinero, Entrada | Comprador y necesidad con reglas fijas; solución potencial con IA, marcada como hipótesis |
| 4. ¿Qué evidencia falta? | Competencia, Dinero, Entrada, Confianza | Lista de verificación automática + preguntas de investigación para la IA externa |

## Detalle de cada pregunta

### 1. ¿Qué cambió realmente?
Clasificación en una o más categorías, con umbrales en `config.yaml`:
- **Cantidad:** empleo, unidades o ingresos reales crecen más que su tendencia previa.
- **Precio:** el índice de precios de la industria crece más que la inflación general.
- **Productividad:** el ingreso real por trabajador o por establecimiento sube con empleo estable.
- **Composición del mercado:** cambia el peso de las subindustrias, el tamaño típico de las
  empresas, la entrada o salida, o la concentración.
- **Regulación:** crecen los documentos del Registro Federal que modifican la parte del código de
  regulaciones aplicable a esa actividad.

Salida: una frase por fenómeno (p. ej., «cambió la productividad, no la demanda») con sus cifras.

### 2. ¿Qué consecuencias genera?
- **Compradores afectados:** quién compra el producto de la industria y en qué proporción
  (insumo-producto).
- **Proveedores afectados:** a quién le compra la industria (insumo-producto).
- **Escasez de capacidad:** vacantes; salarios frente al promedio; ingreso por trabajador.
- **Pérdida de participación:** tasa de salida de empresas; concentración que sube.
- **Márgenes:** ingresos menos gastos, donde exista el dato.
- **Problemas declarados por las empresas:** términos que crecen en la sección de riesgos de los
  reportes anuales de las empresas de esa industria (el texto ya tiene industria desde el
  reprocesamiento del 2026-11-14).

### 3. ¿Dónde puede capturarse valor?
- **Comprador:** el principal según insumo-producto (dato).
- **Necesidad:** el cuello de botella que muestran las preguntas 1 y 2 (dato).
- **Solución potencial:** redactada por IA solo con estas cifras, citando cada una, siempre marcada
  como hipótesis. Solo en la ficha bajo pedido.

### 4. ¿Qué evidencia falta?
**a) Lista de verificación automática** (✅ con dato, 🟡 indirecto, ⚪ sin dato):
- Competencia: número de empresas; participación de las 4 y 8 mayores.
- Tamaño del mercado: ingresos de la industria.
- Accesibilidad: tasa de entrada; capital por establecimiento; carga regulatoria; % de empresas
  pequeñas.
- Señales de disposición a pagar: precio frente a inflación y márgenes (indicio, nunca prueba).

**b) Paquete de preguntas de investigación** para Elicit o AnswerThis:
- Se generan a partir de lo que quedó en 🟡 o ⚪, con el contexto del fenómeno (industria, cifras
  clave, periodo).
- Forma, por ejemplo: «¿Qué evidencia hay de disposición a pagar de [comprador] por [necesidad]
  en [industria] desde [año]?»; «¿Qué soluciones existen hoy para [necesidad] y quiénes las
  ofrecen?».
- Plantillas por **tipo de hueco**, no por tema; las rellenan los datos de cada fenómeno (cumple el
  principio 1 de `CLAUDE.md`).
- En el resumen semanal se generan sin IA; en la ficha bajo pedido la IA puede refinarlas.

## Salidas

- **Resumen semanal (sin IA):** una línea por fenómeno con industria, respuesta a la pregunta 1,
  comprador y consecuencia principal, lista de verificación de la pregunta 4, número de preguntas
  de investigación y enlace al detalle.
- **Detalle por fenómeno (sin IA):** las 9 dimensiones con cifras; las 4 preguntas en lo que se
  responde con reglas fijas; el paquete de preguntas descargable.
- **Ficha profunda (con IA, solo los fenómenos que David elija):** narración de las preguntas 2 y 3
  con citas validadas; toda cifra del texto debe existir en los datos o la ficha no se publica.

## Fuentes

1. **Nueva:** insumo-producto de la Oficina de Análisis Económico (tablas de uso por industria).
2. **Por asociar a cada industria:** índices de precios por industria (Banco de St. Louis y Oficina
   de Estadísticas Laborales).
3. **Disponibles, por conectar:** activos fijos por industria (Oficina de Análisis Económico);
   ingresos y gastos trimestrales por industria (Oficina del Censo); métricas de mercado del
   Bloque 2 (ADR 0024).

## Reglas que se mantienen

- Cero temas escritos a mano: plantillas por tipo de hueco y umbrales en `config.yaml`.
- Nada se descarta: se clasifica y se marca.
- La IA nunca agrega cifras: solo redacta con las que están en los datos.
- El detector estadístico y su control de falsos positivos no cambian.

## Sesiones (una por bloque, con modelo liviano)

| # | Bloque | Terminado cuando |
|---|---|---|
| 0 | Arreglos en curso (fallas 1–5, sesión del 2026-11-17) | Ingesta semanal completa en R2, `text_structure` guardada, lista en observación limpia |
| 1 | Capa 2: las 9 dimensiones como tabla de cifras por fenómeno | Cada fenómeno del reporte tiene sus 9 dimensiones con fuente y fecha; test con residencias de cuidado contra `docs/fichas/2026-11-residencias-cuidado.md` |
| 2 | Pregunta 1: clasificación del cambio | Test sintético por tipo de cambio; residencias de cuidado sale «productividad o composición, no demanda» |
| 3 | Insumo-producto y pregunta 2 | Compradores y proveedores por industria; riesgos de los reportes anuales por industria conectados |
| 4 | Pregunta 4 (lista y paquete de preguntas) + resumen semanal publicado | Todo fenómeno con lista de verificación y preguntas; resumen en Pages |
| 5 | Pregunta 3 + ficha profunda con IA | Ficha de una oportunidad elegida por David con citas validadas |
| 6 | Automatización del domingo | Ingesta → detectores → resumen → publicación sin intervención; ficha profunda solo bajo pedido |

## Decisiones pendientes

- Sesión 4: formato del paquete de preguntas (texto para copiar o tabla descargable).
- Sesión 5: modelo de IA y clave de OpenRouter (`OPENROUTER_API_KEY`).
