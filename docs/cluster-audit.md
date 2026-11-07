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

## Ejecución manual en GitHub Actions y salida privada

Workflow: `.github/workflows/auditoria-clusters.yml` (solo `workflow_dispatch`). Este cambio
prepara la ejecución, **no la ejecuta ni autoriza automáticamente merge, lecturas/escrituras
R2 o despliegues**. Antes de lanzarlo debe existir autorización para leer exclusivamente
`processed/rank` y `processed/series_meta`, y para crear una nueva copia de resultados en
`audits/`. No se leen credenciales localmente ni se solicitan en el chat: el runner usa los
secrets del repositorio ya existentes.

Una vez revisado e incorporado el workflow y autorizada esa operación, el propietario puede
seleccionar Actions → Auditoría interna de clusters → Run workflow, elegir una referencia
revisada y proporcionar `rank_run_id` (exactamente 32 caracteres hexadecimales minúsculos).
El identificador entra mediante variable de entorno y se valida antes de checkout o lecturas.
El workflow tiene permiso GitHub `contents: read`, no publica Pages y no sube artefactos.

Comparte el grupo de concurrencia `reporte`: no se solapa con una mutación del ranking que
respete ese grupo. **No es un snapshot transaccional entre carpetas**: la ingesta de metadatos
puede correr en un grupo distinto. El manifiesto conserva hashes de los archivos locales
leídos, pero no prueba que ranking y metadatos correspondan al mismo instante. Para una
comparación estricta, coordinar una copia congelada con la ingesta fuera de esta herramienta.

Se crean los cinco archivos internos en `work/audit-output` y se copian únicamente al prefijo:

```text
audits/<rank_run_id>/<github_run_id>-<github_run_attempt>/
```

Se usa `rclone copy --immutable --checksum` seguido de `rclone check --one-way --checksum`.
`--immutable` impide que **ese comando** sobrescriba un objeto existente con contenido
incompatible; no habilita Object Lock, retención ni inmutabilidad general en R2. No hay sync,
borrado ni actualización de tablas de producción. Reintentos del workflow usan un prefijo
nuevo mediante `github_run_attempt`; no se sobrescriben etiquetas del propietario.
El workflow no cambia ACL ni habilita endpoints públicos: el bucket de destino debe conservar
su configuración privada; no puede proteger datos si el mismo prefijo ya está expuesto por
una configuración externa.

Los logs de comandos e inventario quedan solo en el runner efímero y no se suben. Al fallar,
se informa un mensaje genérico y código no exitoso; no se imprimen JSON detallados, títulos,
ids de objetos, rutas de archivos de entrada ni credenciales. La salida visible incluye solo
conteos por estado y cobertura NAICS agregada con nombres de fuente/método permitidos.
Nombres desconocidos se omiten y se marca su existencia con un booleano. Se conservan flags
de disponibilidad para distinguir desconocido de cero. Esto permite diagnosticar cobertura
sin divulgar el inventario interno. Los logs visibles de GitHub no son el almacén de etiquetas.

### Recuperación por el propietario

Abrir el prefijo privado en el panel R2 con su cuenta autorizada y descargar los cinco archivos,
o usar su propio terminal confiable con rclone ya configurado para ese bucket. Ejemplo
orientativo (reemplazar las variables por la ejecución concreta, sin compartir credenciales):

```bash
rclone copy "r2:$BUCKET/audits/$RANK_RUN_ID/$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT/" \
  "audit-review-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT" --immutable --checksum
```

Usar un directorio de revisión nuevo. Mantener las etiquetas editadas como copia personal;
no repetir descargas sobre una carpeta con respuestas editadas. El agente no crea credenciales
locales, no utiliza gh ni descarga los resultados privados sin autorización específica.
La existencia del workflow tampoco autoriza automáticamente reintentos con acceso a R2.
