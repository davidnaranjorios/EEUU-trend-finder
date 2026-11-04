# 0009 — Triangulación, deduplicación, ranking y reporte (Fase 5)

- Fecha: 2026-11-03
- Estado: aceptada. David aprobó el plan el 2026-11-03 con las opciones recomendadas A–F (abajo).
  El mismo día el destino de publicación quedó desacoplado (sección 8), mientras se resolvía si el
  repo seguía privado. Después David lo hizo público y activó Pages (fuente "GitHub Actions"):
  el destino por defecto es GitHub Pages.

## Decisiones de David

| | Decisión | Elegido |
|---|---|---|
| A | Modelo de embeddings | `fastembed` (Apache-2.0, ONNX, sin torch) + `BAAI/bge-small-en-v1.5` (MIT) |
| B | Fuentes de los detectores numéricos | FRED, BLS, BEA y Census en **una** familia por detector y corrida |
| C | Series con copyright | Un candidato se publica solo si su evidencia **pública** tiene ≥ 2 fuentes |
| D | Candidatos solo de texto | Sí, en una sección aparte |
| E | Explorador | Las 4 fuentes, fichas mínimas, tope de ~80 MB |
| F | Concurrencia | Grupo propio `reporte` |

Licencias verificadas el 2026-11-03: la licencia de `fastembed` 0.9.1 en PyPI
(`license_expression: Apache-2.0`), y en Hugging Face la de `BAAI/bge-small-en-v1.5` y la de
`Qdrant/bge-small-en-v1.5-onnx-Q` (el ONNX que descarga fastembed), ambas `license:mit`.

## 1. Señales numéricas de las cuatro fuentes

- `detect.source` pasó a ser `detect.sources: [fred, bls, bea, census]`.
- La familia FDR sigue siendo una por detector y por corrida, ahora con todas las fuentes
  (CLAUDE.md). `detector_tests` y `signals` se escriben en la partición de cada fuente;
  `detector_runs`, en la partición `numeric` (las corridas viejas siguen en `fred`).
- Universo: la última decisión del embudo de cada serie en `download.groups`.
  - FRED reclasifica todo el catálogo en cada corrida, así que se toma su última corrida.
  - Las fuentes de la Fase 4 solo reprocesan las unidades que cambian (ADR 0007), así que se toma
    la última decisión por serie.
- El shard se reparte por `fuente|serie`.
- Costo estimado con las mediciones del ADR 0007: unas 410 mil series (FRED 53 k, BLS 300 k,
  BEA 51 k, Census 5 k) dan 1,5–2,5 h con 4 procesos; si no cabe en un job, se reparte con
  `--shard`.

## 2. Fuente independiente (`src/rank/agencies.py`)

Cada evidencia lleva un conjunto de agencias:

| Evidencia | Agencias |
|---|---|
| Serie de FRED | las de sus releases (`fred_series_attrs.agencies`, de `release/sources`) |
| Serie de BLS, BEA o Census | la constante `AGENCY` de su adaptador, con el nombre que usa FRED |
| Término de texto | `texto:edgar` o `texto:federal_register` |

- Los nombres se normalizan: minúsculas, sin puntuación y sin el prefijo "U.S.".
- El número de fuentes independientes es el número de componentes conexas del grafo que une las
  agencias que aparecen en una misma evidencia. Es conservador: el mismo dato de BLS bajado de
  FRED y de BLS cuenta como **una** fuente (la decisión de David en la Fase 4).
- Una serie sin agencia conocida no aporta fuente.
- `rank_runs.params_json.summary.adapter_agencies_unmatched` avisa si el nombre de un adaptador
  no aparece entre las agencias de FRED. En ese caso el solape no se detectaría y hay que
  corregir `AGENCY`.

## 3. Clusters (`src/rank/cluster.py` → `clusters`)

- Universo: series con señal BH en slope o regime.
- Similitud: s = α·max(ρ, 0) + (1 − α)·cos_título.
  - ρ es la correlación de las tasas de crecimiento con la misma preparación de los detectores,
    en la grilla más gruesa del par. Los crecimientos mensuales se suman por trimestre o año
    completo. Con pocos periodos en común, ρ = 0.
  - cos_título es el coseno entre embeddings de "título | unidades".
- Los pares con ρ ≥ 0,98 y cos ≥ 0,9 se toman como copias del mismo dato y se fuerzan a s = 1.
- Agrupación: enlace promedio cortado en s ≥ 0,8. Si hay más de 6.000 series, las de menor q
  entran al clustering y el resto queda como clusters de una serie (se cuentan en `rank_runs`).
- Representante: la serie sin copyright de menor q. El id del cluster es un hash de sus miembros,
  estable entre corridas.
- Los valores α, umbrales y tope son iniciales: se revisan con la primera corrida real.

## 4. Cruces término–serie (`src/rank/links.py` → `series_term_links`)

- Términos: señales BH de `text_emerging`. Series: todas las de `detector_tests` de la corrida.
- **Umbral por término, sacado de los datos.** Es el cuantil 0,95 del coseno de la mejor
  coincidencia contra todas las series, calculado sobre una muestra con semilla fija de 2.000
  términos de `term_freq` sin señal. Un término con señal se enlaza solo si se parece a una serie
  más de lo que se parece a su mejor serie el 95 % de los términos comunes. Además, a lo sumo
  `top_k` = 20 series por término.
- **Cambio respecto del plan** (el plan decía "cuantil 0,999 de pares al azar"):
  - Con unas 400 mil series, un umbral por par deja unas 400 coincidencias por azar por término.
    Casi todo término quedaría enlazado a 20 series y la triangulación perdería sentido.
  - Medido con bge-small: pares sin relación ya dan cosenos de 0,45–0,55.
  - El nulo por término es más conservador y sigue sin mapeos escritos a mano.
- `require_overlap`: la ventana del término tiene que alcanzar la de la señal numérica.
- `method` guarda el modelo, el cuantil, el umbral y `top_k`.
- Caché de vectores por (modelo, sha1 del texto) en `processed/_internal/embeddings/` (R2).
  El primer cálculo es de unos 410 mil títulos: medido a ~265 títulos/s en 4 núcleos, son
  ~26 min. Después solo se calcula lo nuevo.

## 5. Triangulación y score (`src/rank/triangulate.py`)

- Evidencia de un cluster: las series con señal de sus miembros más los términos enlazados a
  cualquiera de ellos.
- Estados del candidato:
  - `candidato`: ≥ `rank.min_sources` (2) fuentes;
  - `una_fuente`: no llega al mínimo;
  - `fuera_del_tope`: pasa el tope de 50 por tipo.
- `published`: candidato cuya evidencia pública también llega al mínimo.
- El validador de `rank_candidates` rechaza cualquier fila que viole estas reglas. También las
  verifica `config.py` (`min_sources` ≥ 2).
- Candidatos solo de texto: términos emergentes con coseno ≥ máx(0,9, nulo) agrupados con enlace
  promedio. Cuentan si sus términos vienen de ≥ 2 fuentes de texto.
- Score = Σ wᵢ·percentilᵢ / Σ wᵢ, con los componentes normalizados por rango dentro de cada tipo.
  Pesos en `rank.weights`:

  | Componente | Peso | Mide |
  |---|---|---|
  | strength | 3 | −log10 q, mediana por fuente independiente |
  | n_sources | 2 | número de fuentes |
  | effect | 1 | efecto |
  | agreement | 1 | slope y regime a la vez |
  | breadth | 1 | entidades del término |
  | size | 0,5 | miembros del cluster (log) |

## 6. Reporte y dashboard (`src/report/`)

- Sitio estático: HTML generado en Python, gráficos SVG en línea y JS propio sin CDN (tooltip y
  explorador).
- `index.html`, el ranking:
  - cada cluster con sus fuentes, sus series (fuente, agencia, detector, q, efecto, ventana y
    gráfico con la ventana sombreada) y sus términos (proporción por periodo, q, entidades y
    similitud);
  - los conteos BH y BY por detector.
- `dashboard.html`:
  - estado del sistema (`runs.jsonl`, `detector_runs`);
  - embudo por fuente;
  - cobertura numérica, de vintages y de texto;
  - explorador de las 4 fuentes (`data/explorer/NN.json`, 128 buckets, tope de 2 MB por archivo
    y 80 MB en total).
- `archivo.html` más `archivo/<fecha>/`: el historial, que vive en `data/reports/` y en R2
  (`reports/`).
- `private_candidates.json` (los candidatos no publicables) va solo a `data/reports/` (R2
  privado), nunca al sitio.

## 7. Copyright, con doble barrera

1. `src/report/data.py` deja fuera toda serie marcada con copyright en `series_meta`, en el
   embudo o en `detector_tests`: no aparece ni su id, ni su título, ni sus valores. El explorador
   la excluye en la consulta.
2. `src/report/guard.py` recorre cada archivo del sitio antes de reemplazar el anterior:
   - extrae los tokens y las cadenas (valores JSON, texto y atributos HTML, puntos de los
     gráficos);
   - falla si aparece el id o el título de una serie con copyright, salvo que también sea de una
     serie pública de otra fuente.

   Si falla, el sitio no se reemplaza y el workflow no publica.

## 8. Publicación desacoplada

- `python -m src.report build` solo construye `data/site/`. El workflow `reporte.yml` lo guarda
  como artefacto y publica según la variable de repositorio `PUBLISH_TARGET`:
  - sin definir o `github_pages` (por defecto): el job `pages` (`upload-pages-artifact` +
    `deploy-pages`). El repo es público y Pages está activado con la fuente "GitHub Actions";
  - `none`: solo artefacto;
  - `cloudflare_pages`: `infra/publish_site.sh` con wrangler fijado (4.151.0). Necesita el
    secret `CLOUDFLARE_API_TOKEN` (permiso Cloudflare Pages: Edit), el secret existente
    `CLOUDFLARE_ACCOUNT_ID` y la variable `CF_PAGES_PROJECT`;
- Límites de Cloudflare Pages: 20.000 archivos y 25 MiB por archivo. El sitio de prueba tiene
  132 archivos, y el explorador real tendría 128 de ≤ 2 MB.

### Registro de corridas propio

Reporte está en el grupo de concurrencia `reporte`, así que puede correr a la vez que Ingesta,
Detectores o Ingesta texto (grupo `ingesta`). Esos tres sincronizan `logs/` entero: si Reporte
también lo sincronizara, `rclone sync` podría pisar `runs.jsonl` y perder corridas.

Por eso Reporte:
- escribe su registro en `logs/reporte/runs.jsonl` (`report.runs_file`);
- sincroniza solo `logs/reporte`;
- el dashboard lee los dos registros.

`tests/test_infra.py::test_only_ingesta_group_syncs_shared_logs` verifica que ningún workflow
fuera del grupo `ingesta` sincronice `logs` entero.

El caso inverso también está cubierto. Cuando `sync_r2.sh` sincroniza `logs`, excluye
`reporte/**`, tanto en la sync como en la verificación. rclone no borra en el destino lo que
queda excluido, así que la ingesta nunca pisa `logs/reporte/` ni lo manda a `_trash`
(`test_sync_of_logs_never_touches_reporte_subdir`).

### Legibilidad del reporte (aprobado por David, 2026-11-04)

- Los gráficos usan las unidades originales en escala lineal. Cada uno lleva título, unidades,
  ajuste estacional, fuente y agencia, más una leyenda de la ventana sombreada y de la fecha PELT.
- Frases en lenguaje simple calculadas desde los datos (`src/report/narrative.py`):
  - crecimiento anual en la ventana frente a la ventana previa;
  - cambio de ritmo en la fecha PELT;
  - proporción de documentos con el término antes y después;
  - por qué el candidato cuenta como ≥ 2 fuentes.
- Una sección "Cómo leer esta página" con glosario (efecto, p, q, BH, BY, fuente independiente
  y score, con los pesos de config.yaml) y tooltips en q, efecto y similitud.
- Sin cambios en la lógica de ranking ni en la regla de fuentes.

### Zoom de 12 meses, descriptivo (aprobado por David, 2026-11-04)

Contexto sobre los datos más recientes (`src/report/recent.py`). No es una señal: no tiene p ni q,
no pasa por FDR y no cambia `rank_candidates` ni el score.

Por serie de frecuencia M, W o D:
- un gráfico de los últimos 24 meses con los 12 últimos resaltados;
- el cambio interanual;
- el ritmo anualizado de los últimos 12 meses frente a la pendiente de 5 años;
- un estado con regla fija sobre el cociente reciente/tendencia (`report.recent`):

  | Estado | Condición |
  |---|---|
  | acelera | ≥ 1,5 |
  | se frena | ≤ 0,5 |
  | revierte | signo contrario y |cociente| ≥ 0,25 |
  | continúa | el resto |

- Q y A dan "dato insuficiente". Q muestra igual el interanual, con aviso, si hay ≥ 4 trimestres.
- Lleva la etiqueta "preliminar".

Por término: la proporción de documentos del último año de la grilla frente al año anterior.

Por candidato: el estado más frecuente entre sus series ("mixto" si hay empate). El sitio filtra y
ordena por estado en JS, sin tocar el score.

## Consecuencias y pendientes

- La primera corrida real necesita, en orden:
  1. detectores con las 4 fuentes (Detectores, grupo `ingesta`);
  2. después, Reporte.
- Los umbrales de cluster y cruces se revisan con esos datos.
- Tiempo de Actions (repo público, minutos sin tope): los detectores pasan de ~1 h a unas 2 h por
  corrida; Reporte toma ~30–40 min la primera vez (embeddings) y unos 15 min después.
