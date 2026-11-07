# Sensibilidad local de señales con ventanas 3/5/7

Este ejecutable de evaluación no cambia detectores, umbrales ni config.yaml. Opera solo con
un snapshot JSON explícito de crecimientos ya preparados. No carga Parquet de producción,
no consulta R2, no reconstruye vintages y no evalúa estabilidad del ranking final.

## Ejecución

```powershell
uv run python eval/window_sensitivity.py prepared.json --output work/result.json --seed 20261020
```

El output debe ser nuevo: se rechaza un archivo existente o el propio input. Se calcula todo
antes de escribir. No se incluyen series, datos ni resultados reales en este PR.

El CLI usa FamilyParams predeterminados y declara todos sus valores en el manifiesto; no lee
config.yaml. Ofrece --seed (obligatorio), --n-resamples y --q. Para replicar otros parámetros
congelados, usar la API evaluate(snapshot, FamilyParams(...), q=...) con esos valores explícitos.
Una ejecución con pocas réplicas es smoke de cableado, no certificación estadística.

## Contrato del snapshot (versión 1)

Objeto JSON con:

- schema_version: 1.
- prepared_for_max_window_years: 7, declaración obligatoria del productor.
- baseline_min_periods: 33; no reducir para admitir series cortas.
- input_manifest_sha256: SHA256 hexadecimal del manifiesto de preparación del productor.
- cutoff: fecha ISO que limita las anclas declaradas.
- anchors: diccionario de las grillas presentes M/Q/A a sus fechas ISO comunes.
- items: lista de objetos con source, object_id, grid, anchor, last_index, growth, scale y
  persistence. last_index sigue grid_index: M=year*12+month-1; Q=year*4+(month-1)//3; A=year.

Cada source/object_id debe ser único. Las series de una grilla deben compartir el ancla;
se exige que no exceda cutoff y que concuerde con last_index. growth es un array unidimensional
finito; scale es finita y positiva; persistence es un entero positivo que cabe en W3.

El productor debe preparar y congelar el mismo historial/escala para los tres escenarios,
filtrar cualquier dato futuro, seleccionar un periodo realmente cerrado, documentar unidades,
transformación, estacionalidad, winsorización, procedencia, exclusiones y hashes en su manifiesto.
El evaluador NO puede demostrar esos pasos a partir del array: el manifiesto de salida dice
expresamente que esa procedencia, cierre de anclas y ausencia de futuro no están verificados.
Verificar fecha declarada <= cutoff no prueba disponibilidad histórica ni cierre del periodo.

Largos mínimos de crecimiento para W7 más la base protegida: M>=168, Q>=61 y A>=40.
M incluye series D/W agregadas previamente por el productor. La escala, persistencia y
arrays se copian, se mantienen inmutables y se reutilizan en los tres escenarios.

## Experimento y salida

Slope compara 3+3, 5+5 y 7+7 años. W equivale a M:36/60/84, Q:12/20/28 y A:3/5/7.
El manifiesto conserva params.window como parámetro recibido, pero identifica expresamente
su reemplazo por escenario/grilla; cada fila informa window_periods efectivo.
La línea base nula usa el historial común anterior a los últimos W periodos.
Regime no se ejecuta: no utiliza slope_window y requeriría otro diseño de truncamiento.

Dentro de cada escenario se ejecutan las grillas con el bootstrap existente, índices
compartidos por subfamilia y parámetros declarados. BH reúne TODAS las fuentes/grillas de
ese escenario antes de corregir; BY es comparación. No se selecciona la mejor ventana ni se
presenta la unión de señales como una nueva familia con FDR controlada al mismo q.

El resultado JSON contiene:

- manifest: hash del snapshot, hash declarado del manifiesto productor, parámetros, semillas,
  corte/anclas, identidades, Python, versiones numpy/scipy y hash del archivo evaluador.
- scenarios: p, q BH/BY, efecto, persistencia, bloque, historia, base nula y ventanas por serie.
- comparisons: intersección/unión, Jaccard, retención y coincidencia de signo entre señales
  compartidas para los pares 3-5, 3-7 y 5-7. Universo único común obligatorio.

Ambos conjuntos vacíos dan Jaccard desconocido (null), nunca estabilidad perfecta.
Sin señales compartidas la concordancia de signo es null. No hay score ni ranking nuevo.

## Controles y límites

controls() genera iid, AR(1), factor común y un positivo de control con cambio sostenido,
con semillas explícitas. Los tests ejecutan controles deterministas con bootstrap pequeño
para verificar cableado, reproducibilidad y detección del positivo. NO estiman ni certifican
FDR. Los controles calibrados existentes de tests/test_detect_fdr.py se conservan intactos.
La campaña posterior deberá fijar tamaño, semillas, parámetros y precisión antes de medir
FDR=P(R>0) bajo nulo global, además de reportar potencia/limitaciones.

Las pruebas cargan el módulo por ruta bajo un nombre neutral: src nunca importa eval/ y el
guard test_importing_src_never_loads_eval permanece intacto. No se implementó el backtest
histórico ni una carga automática de snapshots vigentes. El backtest inicial posterior sigue
limitado a FRED/ALFRED; no se ha verificado una reconstrucción equivalente para otras fuentes.
