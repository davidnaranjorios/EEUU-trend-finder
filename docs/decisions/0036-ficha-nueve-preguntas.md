# ADR 0036 — Ficha: contexto de industria aparte y 9 preguntas literales

Fecha: 2026-11-26. Aprobado por David en su revisión de la corrida `19979e2620e64e45beae7b9f420c39ab`.
Base: ADR 0029 (dimensiones), 0030 («qué cambió») y 0032 (consecuencias, insumo-producto).

## Decisiones
1. **Contexto de industria** (bloque aparte, fuera de las 9): CR4/CR8, empresas, ingresos, entrada y
   salida (y su referencia de la economía total), nivel frente al pico y precio frente al deflactor.
2. **Cada dimensión responde su pregunta literal** (`schemas.Question`, un `Enum` como `Dimension`, ADR 0029 punto 8: el guard
   de listas literales no se toca ni se esquiva; se muestran así en el reporte):

   | Dimensión | Pregunta | Evidencia que la responde |
   |---|---|---|
   | Fenómeno | ¿Qué cambió? | filas de «qué cambió» (cantidad, precio, productividad, composición) |
   | Evidencia | ¿Qué lo prueba? | series con q ≤ q de FDR; ≥ 2 fuentes independientes = demostrado |
   | Demanda | ¿Quién necesita algo como consecuencia? | solo compradores (insumo-producto, ADR 0032) |
   | Brecha | ¿Qué no resuelve la oferta? | escasez (vacantes, nómina, productividad) y «posible escasez de personal» |
   | Competencia | ¿Quién la atiende hoy? | composición del mercado y pérdida de participación |
   | Dinero | ¿Cuánto gastan hoy los compradores en esa necesidad? | gasto de los compradores en dólares (`gasto_total`, `gasto_comprador_i`, filas nuevas de «compradores») |
   | Entrada | ¿Qué cuesta entrar? | ingreso por establecimiento/empresa y márgenes |
   | Riesgo | ¿Qué puede salir mal? | problemas declarados (sobrerrepresentación en 10-K de la industria) |
   | Confianza | ¿Cuánto de esto está demostrado? | niveles de las otras ocho preguntas |

   Reglas: sin cifra que responda → **«sin evidencia»**; las cifras de Evidencia no se reutilizan en
   Demanda; Dinero es gasto de compradores, no ingresos de la industria (esos van en Contexto).
3. **Niveles por pregunta:** demostrado ✅ (alguna cifra directa; Evidencia: ≥ 2 fuentes
   independientes), indicio 🟡 (solo cifras indirectas, o una sola fuente), hipótesis ⚪ (solo la marca
   «posible escasez de personal», inferida del empleo, sin cifra de escasez), sin evidencia (—).
4. **Confianza** cuenta los niveles de las otras ocho preguntas (no cifras). No tiene nivel propio:
   sería circular. (El texto del encargo dice «las 9»; se cuentan 8 y se documenta.)
5. **Productividad con empleo en caída.** Si el empleo de la industria cae más que
   `rank.change.employment_drop_pct_year` (2 %/año, config), «productividad» no es «sí» aunque el
   ingreso real por trabajador suba. Nota «posible escasez de personal» en «¿qué cambió?» (frase,
   fila) y en Brecha (como hipótesis, o indicio si hay cifras de escasez).

## Interpretaciones a revisar
- Con CR4/CR8, empresas y tasas de entrada en el Contexto, **Competencia** se responde con la
  composición del mercado (`composicion`) y la pérdida de participación; **Entrada** con el tamaño
  típico (ingreso por establecimiento/empresa) y márgenes. Si prefieres que esas cifras también
  cuenten dentro de Competencia/Entrada, es un cambio de una línea en `ficha.build`.
- Las tablas `phenomenon_dimensions/change/consequences` no cambian de esquema (solo filas nuevas de
  gasto en `compradores`); la ficha se compone al construir el reporte, solo con filas públicas.
  Las filas «confianza» antiguas de `phenomenon_dimensions` quedan como legado y no se muestran.

## Límites
- Sin `market/io_flows` en R2 (la corrida de referencia no lo tiene) Demanda y Dinero salen «sin
  evidencia» para todos los fenómenos, incluido 6231.
