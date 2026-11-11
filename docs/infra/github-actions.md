# Ingesta en GitHub Actions (ADR 0004)

Tres pasos manuales, una sola vez. Después corre sola cada domingo.

## 1. Bucket de R2

En el panel de Cloudflare: **R2 Object Storage → Create bucket**. Si no se llama
`trend-finder` (el valor de `storage.bucket` en `config.yaml`), anota el nombre para el
paso 3.

## 2. Credencial de acceso al bucket

En **R2 → Manage API tokens → Create API token**:

- Permiso: **Object Read & Write**.
- Alcance: **Apply to specific buckets only**, y elige solo tu bucket.

Al crearlo, Cloudflare muestra tres valores, una sola vez: cópialos.

| Lo que muestra Cloudflare | Nombre del secret en GitHub |
|---|---|
| Access Key ID | `AWS_ACCESS_KEY_ID` |
| Secret Access Key | `AWS_SECRET_ACCESS_KEY` |
| Account ID (también visible en la portada de R2) | `CLOUDFLARE_ACCOUNT_ID` |

## 3. Secrets en GitHub

En el repositorio: **Settings → Secrets and variables → Actions → New repository secret**.
Crea cuatro, con el nombre exacto:

- `FRED_API_KEY`: tu clave de FRED.
- `CLOUDFLARE_ACCOUNT_ID`, `AWS_ACCESS_KEY_ID` y `AWS_SECRET_ACCESS_KEY`: los del paso 2.

Si el bucket tiene otro nombre: en la pestaña **Variables → New repository variable**,
crea `R2_BUCKET` con ese nombre.

## 4. Primera corrida

**Actions → Ingesta → Run workflow**. La primera corrida toma ~4–5 h: catálogo, metadatos,
embudo, descarga v2 y muestra de vintages. Al terminar, la página de la corrida muestra un
resumen, y en R2 aparecen `processed/`, `exports/` y `logs/`. Si se corta por el límite de 6 h,
vuelve a lanzarla: continúa donde quedó.

A partir de ahí corre sola los domingos a las 06:13 UTC.

## 5. Regla de expiración de `_trash/` (una vez)

Cada sync manda a `_trash/<fecha>/` lo que borra o reemplaza en R2: la compactación reemplaza
los Parquet que cambiaron cada semana (~0,5 GB). Sin una regla, `_trash/` crece sin límite y se
come la capa gratuita (10 GB). Con 30 días quedan ~2–2,5 GB como respaldo.

Pasos (documentación de Cloudflare "Object lifecycles", verificada el 2026-11-03):

1. En el panel de Cloudflare: **R2 object storage → Overview** y elige el bucket
   (`trend-finder`).
2. Pestaña **Settings** → sección **Object Lifecycle Rules** → **Add rule**.
3. Completa la regla:
   - nombre: `expirar-trash`;
   - alcance: **solo objetos con el prefijo** `_trash/` (con la barra final; nunca el bucket
     entero, que borraría los datos);
   - acción: **borrar objetos** (*delete uploaded objects*) **30 días** después de subidos.
4. **Save changes**.

Cloudflare borra los objetos vencidos normalmente dentro de las 24 h siguientes; para objetos
que ya existían al crear la regla puede tardar más.

**Comprobar:** la regla aparece en la lista de *Object Lifecycle Rules* con el prefijo `_trash/`
y 30 días. El token S3 de la ingesta (*Object Read & Write*) no puede leer ni cambiar reglas
(eso pide el permiso *Workers R2 Storage Write*), así que la comprobación es en el panel. El
workflow "Verificar R2" muestra el tamaño de `_trash/`, que debe dejar de crecer pasado un mes.

Alternativa por línea de comandos, con un token de Cloudflare que tenga ese permiso:
`npx wrangler r2 bucket lifecycle list trend-finder`.

## Consultar los datos

Desde cualquier máquina con los tres valores de R2 en un `.env`:

```
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
from src.catalog import connect_r2; from src.config import load_config
con = connect_r2(load_config()['storage'])
print(con.sql('SELECT group_name, count(*) FROM eligibility GROUP BY 1'))"
```

## Fuentes de la Fase 4: presupuesto y cortes (ADR 0019)

`infra/sources_update.sh` corre BLS, BEA y Census después de FRED. Cada fuente tiene un presupuesto de
60 min (`SOURCES_BUDGET_MIN`) que se respeta también dentro de cada unidad, un watchdog en el proceso y un
`timeout` por fuente (presupuesto + `SOURCES_GRACE_MIN`, 15). Una fuente colgada se mata, se sincroniza a R2
lo que dejó hecho y se sigue con la siguiente; la sincronización también tiene `timeout`
(`SOURCES_SYNC_TIMEOUT_MIN`, 30). `SOURCES_TOTAL_MIN` (240) reparte el tiempo entre fuentes. Lo cortado se
reanuda en la corrida siguiente. En el log, buscar `unidad iniciada`, `unidad en progreso` (con `rss_mb`) y
`unidad terminada` (con `lectura_s` y `escritura_s`); `units_interrupted` en el resumen cuenta las unidades que
no cupieron.
