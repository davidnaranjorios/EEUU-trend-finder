# ADR 0014 — Clase de medida: precio, cantidad, valor, tasa u otro

Fecha: 2026-11-10. Aprobada por David el mismo día, con esta condición: la tabla de reglas cuenta
como **metadatos de agencia, no como temas**, siempre que solo contenga unidades, prefijos de
encuesta y tipos de tabla oficiales (nunca industrias, productos ni palabras clave), con un test
que lo verifique. No amplía ninguna excepción del principio 1: el guard no escanea `config/` y
acepta el archivo tal como está (verificado por test).

## Problema
Un fenómeno cuyo respaldo son solo precios puede reflejar costos u oferta y no demanda. El sistema
no sabía qué mide cada serie. Medición del 2026-11-10 sobre las 315 señales de la corrida b23cf89a
(por título, aproximada): precios 35 %, balances financieros 23 %, salarios y costo laboral 19 %,
población 11 %, cantidades 7 %.

## Decisión
- **Clase** de cada serie: `precio`, `cantidad`, `valor` (nominal = precio × cantidad), `tasa`
  (razón o porcentaje) u `otro`. Subtipo `costo_laboral` para el precio del trabajo (salarios y
  compensación por hora, índice de costo laboral): cuenta como `precio`.
- **Tabla de reglas** `config/measure_kind.yaml` (`rank.measure_kind.table`), con esquema validado
  en `src/rank/measure.py`: una regla es `match` (AND de `source`, `id_regex`, `units_regex`,
  `title_regex`) más `kind`. Gana la primera que casa; sin regla, `otro`. Orden: tasas, precio del
  trabajo, precios, cantidades, valor nominal, conteos.
  - **Fuentes de la clase:** unidades («Index», «Chained dollars», «Percent», «Thousands of
    persons», «Dollars per hour»); prefijos oficiales de las encuestas de precios de BLS (CPI-U,
    CPI-W, C-CPI-U, PPI por industria y por producto, importación y exportación, índice de costo
    laboral); y descriptores del tipo de tabla o medida que ya trae el título («Price Indexes»,
    «Chain-Type Quantity Indexes», «Percent Change», «Value Added»).
  - **Qué no contiene (verificado por `tests/test_measure.py`):** cada palabra de cada expresión
    pertenece a un vocabulario cerrado de descriptores de medida y unidades; los `id_regex` solo
    admiten prefijos de BLS; y ningún nombre oficial corto de industria de NAICS aparece en
    ninguna expresión.
- **Columnas aditivas y nulas** (las particiones anteriores quedan con NULL): `measure_kind` y
  `measure_subtype` en `rank_evidence`; `measure_mix_json` (cuántas series de cada clase) en
  `rank_candidates` y `rank_phenomena`.
- **Etiqueta `sin_evidencia_de_demanda`** (`rank_phenomena.no_demand_evidence`): verdadera cuando
  **todo** el respaldo numérico del fenómeno son series `precio`. Falsa si hay alguna `cantidad`,
  `valor` o `tasa`. **NULL si alguna serie es `otro`**: con una serie desconocida no se puede
  afirmar que falte demanda. Los términos de texto no cuentan. **Solo etiqueta**: no filtra ni
  cambia el score (el score lo decide David después).
- **Reporte:** la clase de cada serie en su ficha, la mezcla de clases en cada fenómeno y la
  advertencia «Sin evidencia de demanda» en los fenómenos que la tienen; glosario.
- **Cobertura** en `rank_runs.params_json.summary.measure_kind_coverage`: por fuente, series con
  señal, clasificadas (distintas de `otro`), porcentaje y conteo por clase. Meta: ≥ 90 % de las
  series con señal. El valor real se informa después de la primera corrida; los tests solo
  comprueban casos con ids, unidades y títulos reales de cada fuente.

## Límites
- **La cobertura real no está medida.** No tengo los metadatos de la corrida; las reglas se
  escribieron contra unidades y títulos reales conocidos de FRED, BLS y BEA. Se espera que
  **Census quede casi todo en `otro`**: sus series no traen unidades y las variables EITS
  (`data_type_code`) no tienen una tabla oficial de clases verificada; ampliar eso requiere leer
  la tabla de tipos de dato de cada dataset en la ingesta.
- Un índice sin descriptor en título ni unidades («Index 2017=100» de una producción industrial)
  queda `otro`: distinguir cantidad de precio ahí exigiría una palabra de tema.
- Los salarios semanales y anuales (no por hora) quedan `otro`: el encargo definió el precio del
  trabajo como salario o compensación **por hora**.
- Los sufijos de tipo de dato de BLS CES (`…03`, `…08`) no se usan: no pude verificarlos y las
  unidades de la tabla de BLS ya los distinguen.

## Cambios posteriores
- 2026-11-13: regla `valor_nomina` (`title_regex: (?i)\bpayrolls?\b`, `kind: valor`) antes de los conteos.
  Las series CES de nómina («Aggregate weekly payrolls…», «Indexes of aggregate weekly payrolls…»)
  caían en `cantidad_conteos` por la unidad «thousands», pero son dólares (nota B de la ficha 001).
