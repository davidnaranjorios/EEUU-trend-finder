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

## Consultar los datos

Desde cualquier máquina con los tres valores de R2 en un `.env`:

```
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
from src.catalog import connect_r2; from src.config import load_config
con = connect_r2(load_config()['storage'])
print(con.sql('SELECT group_name, count(*) FROM eligibility GROUP BY 1'))"
```
