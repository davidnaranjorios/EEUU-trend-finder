# Inventario interno de clusters y muestra para revisión

La herramienta no consulta R2 ni APIs, no ejecuta detectores o ranking y no cambia umbrales.
Usa una **copia local previamente autorizada** de las tablas del ranking. No hay resultados
reales incluidos en el repositorio ni se presupone que una corrida tenga 119 clusters.

```powershell
uv run python eval/cluster_audit.py --rank-dir data/processed/rank --run-id ID --meta-dir data/processed/series_meta --out work/audit-ID
```

`--meta-dir` es opcional. Si se proporciona, solo se leen los metadatos de miembros conocidos;
los títulos y unidades que no estén conservados permanecen desconocidos. Sin títulos, la
coherencia puede ser no evaluable; el propietario decide y puede consultar su copia de evidencia.
No se descargan series, texto, embeddings ni modelos. No se consulta la configuración actual
para atribuir a una corrida histórica umbrales que no quedaron registrados.

La salida debe estar fuera del árbol de entradas y en un directorio nuevo:

- `inventario.json`: miembros, títulos cuando existen, evidencia, fuentes/adaptadores,
  origen y cadena, NAICS y método, enlaces, estado y regla de exclusión persistida.
- `muestra.json`: hasta 20 registros, incluyendo un publicado si existe y recorriendo
  estratos de estado y cobertura NAICS; orden SHA256 con semilla explícita.
- `etiquetas.csv`: campos vacíos para que el propietario etiquete coherencia, vínculo
  texto–serie, independencia y NAICS, con motivo.
- `rubrica.md`: opciones correcto / dudoso / incorrecto / no_evaluable y límites.
- `manifest.json`: corrida, hashes de archivos, tablas faltantes, conteos, muestreo y límites.

Se distingue miembro del cluster de evidencia contextual mediante `in_cluster`, y se marca
si no se conservó membresía (`membership_recorded`). Los candidatos/evidencia sin registro
correspondiente en clusters siguen siendo visibles como inconsistencias o datos incompletos;
por eso el manifiesto distingue cantidad de registros del inventario y clusters persistidos.

Las exclusiones interpretables son las que el ranking conserva: `una_fuente` (triangulación),
`fuera_del_tope` y candidato no publicado por falta de fuentes públicas. La tabla de corridas
provee los umbrales cuando existe. Si faltan datos, el umbral o estado permanece desconocido.
No se inventa por qué un par no obtuvo enlace: no se guardaron todos los pares rechazados.
Un `validated=false` no distingue falta de prueba, ausencia de solapamiento y prueba fallida.
En archivos antiguos sin campo `validated`, se muestra `no_registrado`.

**Uso interno, no publicable.** La salida puede contener ids, títulos o evidencia con copyright;
no incluye valores de series, no elimina la barrera del reporte y no debe copiarse a `data/site`
ni a `reports`. Los metadatos preservan la marca de copyright cuando está disponible; su ausencia
no implica permiso de publicación. La herramienta no sobrescribe etiquetas existentes.

Las etiquetas corresponden al propietario, no al modelo. La muestra estratificada es diagnóstica;
no debe convertirse en precisión poblacional sin ponderaciones y diseño de evaluación apropiado.

## Cobertura NAICS por fuente y método

`manifest.json.naics_coverage` usa todas las filas de `naics_links` de la corrida seleccionada,
no solo miembros de clusters o candidatos. Cuenta objetos únicos por fuente y método,
deduplicando códigos y filas del mismo objeto. Los métodos se superponen: **no sumar sus
conteos** para obtener el total. No se suman pesos ni se presenta el número de filas como
número de objetos.

El denominador, cantidad de señales con NAICS y porcentaje se conservan desde
`rank_runs.params_json.summary.naics_coverage`. Si ese resumen no existe, se muestran como
desconocidos; no se estiman a partir del inventario. Los objetos de links pueden pertenecer
a un universo distinto del de señales, por lo que no se divide su conteo por ese denominador.
Si falta la tabla de links, sus conteos también permanecen desconocidos, no cero.
