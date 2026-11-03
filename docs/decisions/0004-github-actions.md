# 0004 — GitHub Actions + R2 en lugar del VPS

- Fecha: 2026-11-02
- Estado: aceptada. Reemplaza al ADR 0003, cuyo VPS queda como alternativa documentada.

## Contexto

El ADR 0003 eligió un VPS porque la descarga con la API v1 tomaba días. Con la API v2 (ADR
0002), bajar las 53.126 series del grupo nacional toma ~20 min. La corrida inicial completa
(catálogo ~2 h, metadatos ~1 h, v2 ~20 min, muestra de vintages ~1 h) cabe en el límite de 6 h
por job de GitHub Actions, y la semanal toma menos de una hora. El VPS obligaba a manejar seis
credenciales (llave SSH, deploy key, token de Hetzner, token de API de Cloudflare, FRED, S3 de
R2), mantener un servidor y pagar ~€42/mes.

## Decisión

- La ingesta corre en `.github/workflows/ingesta.yml`, los domingos a las 06:13 UTC y a mano
  (`workflow_dispatch`).
  - `concurrency` impide dos corridas a la vez.
  - Cada corrida:
    1. trae el estado y los logs desde R2 (`infra/pull_r2.sh`, `rclone copy`, nunca borra
       en local);
    2. corre `infra/run_actions.sh`, que continúa la descarga inicial (`run_tramos.sh`)
       mientras no exista `initial_download.done`, y después la actualización semanal
       (`weekly_update.sh`);
    3. sincroniza a R2 tras cada tramo (`infra/sync_r2.sh`).
- **R2 sigue siendo el almacenamiento definitivo** (ADR 0002). Hace falta porque la máquina de
  Actions se borra al terminar cada corrida. El grupo nacional ocupa ~1 GB, dentro de la capa
  gratuita (10 GB).
- **Credenciales:** cuatro *secrets* del repositorio, `FRED_API_KEY` y la credencial S3 de R2
  (`CLOUDFLARE_ACCOUNT_ID`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`). El bucket se crea a
  mano en el panel de Cloudflare, sin token de API. Variable opcional: `R2_BUCKET`.
- Los scripts de reanudación (`run_tramos.sh`, `weekly_update.sh`, `sync_r2.sh`) son los mismos
  del VPS. `infra/r2_env.sh` lee `.env` si existe y, si no, el entorno.
- Los logs (`data/logs/runs.jsonl`) también van a R2. Además, cada corrida sube `data/logs/`
  como artefacto por 30 días y escribe un resumen en la página de la corrida.

## Actualización 2026-11-03 (bloque 0 de docs/plan.md)

- **Compactación.** La corrida inicial dejó 14.491 Parquet (498,5 MB) en `processed/`. La razón:
  `store.upsert` escribe un `part-*.parquet` por llamada y por bucket tocado, y nunca los junta.
  La descarga v2 hace un upsert por página (357 páginas, cada una toca hasta 64 buckets); los
  vintages, uno por lote de 200 series, y el estado, otro. Leer esos archivos uno a uno por
  httpfs no terminó en 30 min. `python -m src.ingest.store compact` junta cada bucket en un solo
  archivo con el mismo swap atómico de `upsert` (recuperable) y comprueba que el número de filas
  no cambie; los buckets que ya tienen un archivo no se tocan. `infra/sync_r2.sh` compacta antes
  de cada subida. Queda ≤ 1 archivo por bucket (≈ tablas × 64), y un test verifica que una
  segunda corrida no aumenta el número de archivos. "Verificar R2" vuelve a medir la lectura
  directa por httpfs, cronometrada y con límite de 15 min.
- **Costo de la compactación en R2.** Cada semana se reescriben los buckets que cambiaron
  (~0,5 GB) y lo reemplazado va a `_trash/`. Una regla de ciclo de vida de R2 borra `_trash/`
  a los **30 días** (decisión 6 de docs/plan.md; guía en `docs/infra/github-actions.md`, paso
  5). En régimen: ~0,5 GB de datos + ~2–2,5 GB de `_trash/`, dentro de los 10 GB gratuitos.
- **Exportes con tope.** Pasaban de 66 archivos y 101,4 MB: fichas de 245k series de cuatro
  grupos, con las claves repetidas en cada ficha. Ahora:
  - solo hay fichas del grupo `exports.groups` (nacional, decisión del 2026-11-03);
  - el formato es columnar y las categorías van por índice a `categories.json`;
  - hay un tope de `exports.max_file_kb` (512) por archivo y `exports.max_total_mb` (20) en
    total; si se pasa, el export falla sin escribir.

  La corrida igual sincroniza los datos y termina en error, para que un exporte no bloquee
  la ingesta.
- **Acciones en Node 24.** `actions/checkout@v6`, `astral-sh/setup-uv@v7` y
  `actions/upload-artifact@v6`: las tres declaran `runs.using: node24` en su `action.yml`.
  Antes eran checkout@v4, setup-uv@v6 y upload-artifact@v4, que corrían en Node 20 y daban el
  aviso de deprecación. Un test impide volver a versiones no listadas.

## Límites y riesgos

- **6 h por job:** si una corrida se corta, la siguiente continúa desde el último tramo
  sincronizado. La excepción son `crawl` y `crawl-meta`, que no son reanudables a mitad de
  camino porque su caché raw no se sube a R2: si se cortan, se repiten. Hoy suman ~3,3 h.
- **Minutos de Actions:** un repositorio privado del plan Free tiene 2.000 min/mes gratis (dato
  de terceros, a verificar en docs.github.com). Uso estimado: ~270 min la corrida inicial y
  ~400–500 min/mes en régimen (4 semanales y un crawl mensual).
- **Programación:** GitHub solo ejecuta los `schedule` del workflow en la rama por defecto
  (`main`). En repositorios públicos, además, desactiva los programados tras 60 días sin
  actividad; este repositorio es privado.

## Consecuencias

- No hay servidor que mantener ni llaves SSH.
- `infra/provision.sh`, `infra/systemd/*`, `infra/hcloud_create.sh` y `docs/infra/vps.md`
  quedan como alternativa si algún día la ingesta deja de caber en 6 h (p. ej., al sumar todos
  los grupos o las fuentes de la Fase 4).
