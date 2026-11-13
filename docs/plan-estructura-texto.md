# Plan: conocer la estructura de los documentos de texto antes de buscar

Propuesto el 2026-11-13, pendiente de aprobación de David.

## Qué se hace hoy (verificado en los datos de R2)

| Fuente | Qué se guarda | Qué se ignora |
|---|---|---|
| Reportes anuales de empresas (Comisión de Bolsa y Valores) | Solo 2 secciones: factores de riesgo (1A) y discusión de la gerencia (7), cortadas en frases de 1–3 palabras. Entidad = número de la empresa. | El encabezado del reporte (industria de la empresa, estado, tamaño, cierre fiscal), las demás secciones, los estados financieros estructurados. **0 de 2,6 millones de frases tienen industria.** |
| Registro Federal | Tipo de documento (regla, propuesta de regla, aviso, documento presidencial) y agencia. Ejemplo marzo 2025: 1.805 avisos, 181 reglas, 103 propuestas, 41 presidenciales. | La parte del código de regulaciones que modifica cada documento (que indica a quién regula), si la regla es «significativa», fechas de vigencia, temas oficiales de indexación. |

## Etapa 1 — Inventario de estructura (solo describir, sin detectar nada)

Para cada fuente, una tabla por año con qué campos existen y qué porcentaje de documentos los trae.

**Reportes anuales de empresas**
1. Encabezado: código de industria de 4 dígitos, estado, cierre del ejercicio, categoría de tamaño
   del emisor (grande, mediano, pequeño).
2. Secciones: cuáles aparecen cada año (1, 1A, 1B, 1C, 2, 3, 7, 7A, 8…) y su largo típico.
   Interesa en especial **cuándo nacen secciones por mandato**: factores de riesgo obligatorios
   desde 2005, ciberseguridad (1C) desde 2023. Un cambio de formulario genera términos «nuevos»
   que no son tendencia.
3. Estados financieros estructurados que la Comisión publica por empresa (ingresos, activos,
   segmentos): si existen, dan números por empresa e industria sin leer texto.
4. Cuántas empresas hay por industria y año: si una industria tiene 3 empresas, no hay base para
   buscar términos en ella.

**Registro Federal**
1. Tipo de documento por año.
2. Agencia y subagencia.
3. Parte del código de regulaciones afectada (título y parte), que permite saber qué actividad se
   regula.
4. Marca de regla significativa e impacto económico declarado.
5. Temas oficiales de indexación que asigna la propia agencia (no son una lista nuestra).
6. Cambios de formato del archivo a lo largo del tiempo (ya se detectó uno en 2024).

**Entregable:** `docs/estructura-texto.md` con las tablas de cobertura por año y la lista de
cambios de formato, para que David lo revise. **No se cambia ningún detector en esta etapa.**

## Etapa 2 — Definir las unidades de análisis (con la aprobación de David)

Con el inventario a la vista se decide cómo se segmenta antes de buscar, por ejemplo:
- reportes de empresas: industria de la empresa × sección (riesgos / gerencia / negocio);
- Registro Federal: tipo de documento × parte del código de regulaciones (o agencia).

Regla mínima: un segmento entra solo si tiene suficientes documentos por año (umbral en
`config.yaml`, medido en la etapa 1).

## Etapa 3 — Detección dentro de cada segmento

Un término «emerge» si su proporción crece **dentro de su segmento**, comparado con la historia
del mismo segmento. Además:
- años con cambio de formulario o formato se marcan y no cuentan como crecimiento;
- cada término queda con industria (por las empresas que lo usan o por la parte del código de
  regulaciones), y recién entonces se intenta conectarlo con las series numéricas de esa industria.

## Costo

- Etapa 1: una corrida de muestra (unos cientos de documentos por año) y luego el inventario
  completo dentro del reprocesamiento único (~13 h, partido en tramos de menos de 6 h).
- Después, la corrida semanal procesa solo documentos nuevos.
