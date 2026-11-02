# Guía del VPS (ADR 0003)

Pasos manuales para dejar corriendo la ingesta en un servidor Hetzner con almacenamiento
definitivo en Cloudflare R2. Todo lo que no está aquí lo hace `infra/provision.sh`.

**Antes de empezar:** decide los umbrales del embudo en `config.yaml`, haz commit y push.
Los umbrales son `eligibility.*` y los grupos son `download.groups` y `vintages.*`. El
servidor clona el repo y baja solo lo que diga esa configuración.

## 1. Llave SSH (en tu computadora)

```
ssh-keygen -t ed25519 -C "eeuu-trend-finder" -f ~/.ssh/eeuu_trend
```

Guarda `~/.ssh/eeuu_trend` en privado; la que se sube es `~/.ssh/eeuu_trend.pub`.

## 2. Servidor y volumen

**Opción A, por API (recomendada):** en la consola de Hetzner, entra al proyecto y ve a
**Security → API tokens → Generate API token** (permiso *Read & Write*). Después:

```
HCLOUD_TOKEN=<token> SSH_PUBKEY_FILE=~/.ssh/eeuu_trend.pub bash infra/hcloud_create.sh
```

El script crea (o reutiliza, si ya existen) la llave SSH, un firewall con solo el puerto 22,
un volumen de 100 GB y el servidor CPX32 con Ubuntu 24.04 en `nbg1`, con el volumen adjunto
sin automontaje. Imprime `IP=` y `VOLUME_ID=` para el paso 5. Se puede cambiar con
`LOCATION=`, `SERVER_TYPE=` o `VOLUME_GB=`.

**Opción B, en la consola de Hetzner Cloud:**


1. **Security → SSH Keys → Add SSH key:** pega el contenido de `eeuu_trend.pub`.
2. **Servers → Add server:**
   - Ubicación: una de la UE (p. ej. Falkenstein o Nuremberg).
   - Imagen: Ubuntu 24.04.
   - Tipo: **CPX32** (4 vCPU, 8 GB). Revisa el precio vigente.
   - SSH key: la del paso anterior.
   - Sin contraseña de root.
3. **Volumes → Create volume:**
   - 80–100 GB, en la **misma ubicación** que el servidor, adjunto a él.
   - Montaje: **Manual**. `provision.sh` lo formatea solo si está vacío y lo monta en
     `/mnt/data`.
   - Anota el **ID del volumen** (número en la URL o en la ficha del volumen).
4. **Firewalls (opcional, además de `ufw`):** regla de entrada solo para TCP 22.

## 3. Bucket y llaves de R2 (panel de Cloudflare)

1. **Bucket:** `trend-finder`, el mismo que `storage.bucket` en `config.yaml`. Por API,
   con un token de cuenta con permiso *Workers R2 Storage: Edit*:
   ```
   CLOUDFLARE_API_TOKEN=<token> CLOUDFLARE_ACCOUNT_ID=<id> bash infra/r2_create_bucket.sh
   ```
   O a mano: **R2 Object Storage → Create bucket**.
2. **R2 → Manage API tokens → Create API token:**
   - Permiso: **Object Read & Write**.
   - Alcance: **solo el bucket `trend-finder`**.
   - TTL a tu criterio.
3. Copia, porque solo se muestran una vez:
   - **Access Key ID** → `AWS_ACCESS_KEY_ID`.
   - **Secret Access Key** → `AWS_SECRET_ACCESS_KEY`.
   - **Account ID** (aparece en la URL del endpoint `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`)
     → `CLOUDFLARE_ACCOUNT_ID`.

## 4. Acceso del servidor al repositorio

Si el repositorio es público, usa la URL `https://github.com/<owner>/<repo>.git` y salta este
paso. Si es privado, usa una deploy key de solo lectura:

1. En el servidor, como root:
   ```
   id trend >/dev/null 2>&1 || useradd --create-home --shell /bin/bash trend
   sudo -u trend ssh-keygen -t ed25519 -N "" -f /home/trend/.ssh/id_ed25519
   cat /home/trend/.ssh/id_ed25519.pub
   ```
2. En GitHub: **repo → Settings → Deploy keys → Add deploy key**, pega la llave y deja
   *Allow write access* sin marcar.
3. Usa `REPO_URL=git@github.com:<owner>/<repo>.git` en el paso 5. La primera vez acepta la
   huella de github.com:
   ```
   sudo -u trend ssh -T git@github.com
   ```

## 5. Aprovisionar

```
ssh -i ~/.ssh/eeuu_trend root@<IP>
git clone https://github.com/<owner>/<repo>.git /root/eeuu && cd /root/eeuu   # o copia infra/provision.sh
SSH_PUBKEY_FILE=/root/.ssh/authorized_keys \
VOLUME_ID=<ID del volumen> \
REPO_URL=<URL del paso 4> \
BRANCH=<rama con tu config> \
bash infra/provision.sh
```

Al terminar, **root ya no puede entrar por SSH**. Desde otra terminal, verifica que entras como
`ops` **antes** de cerrar la sesión de root:

```
ssh -i ~/.ssh/eeuu_trend ops@<IP>
```

El script se puede volver a correr cuando quieras (como `ops`, con `sudo -E`); no repite lo que
ya está hecho.

## 6. Secretos y arranque

```
sudo -u trend nano /home/trend/app/.env
```

Rellena `FRED_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `AWS_ACCESS_KEY_ID` y
`AWS_SECRET_ACCESS_KEY`. El archivo queda con modo `600`; compruébalo con
`ls -l /home/trend/app/.env`.

Prueba el acceso a R2 y a FRED antes de lanzar nada largo:

```
sudo -u trend bash -lc 'cd ~/app && uv run python -m src.ingest.fred crawl --if-stale'
```

El primer `crawl` tarda ~2 h y el `crawl-meta` ~1 h. Puedes dejar que los haga el servicio.

Lanza la descarga inicial y activa la actualización semanal:

```
sudo systemctl enable --now trend-initial-download.service
sudo systemctl enable --now trend-weekly-update.timer
```

## 7. Verificar

```
systemctl status trend-initial-download.service
journalctl -u trend-initial-download.service -f              # logs JSON por línea
tail -n 3 /home/trend/app/data/logs/runs.jsonl               # una fila por corrida
cat /home/trend/app/data/processed/ingest_state/initial_funnel.json
systemctl list-timers trend-weekly-update.timer
```

Consulta directa en R2 (desde cualquier máquina con el `.env`):

```
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
from src.catalog import connect_r2; from src.config import load_config
con = connect_r2(load_config()['storage'])
print(con.sql('SELECT group_name, count(*) FROM eligibility GROUP BY 1'))"
```

## Operación

- **Reanudar tras un corte:** no hay que hacer nada. `Restart=on-failure` reintenta cada 10 min.
  A mano: `sudo systemctl restart trend-initial-download.service`.
- **Cambiar umbrales después de la descarga inicial:**
  1. Edita `config.yaml`, haz commit y luego `git pull` en el servidor.
  2. La corrida semanal recalcula el embudo y baja las series que entren.
  3. Para repetir la descarga inicial completa, borra
     `data/processed/ingest_state/initial_funnel.done` y `initial_download.done`.
- **Recuperar algo borrado en R2:** el sync mueve lo reemplazado a `_trash/<fecha>/` dentro del
  bucket.
- **Rotar llaves de R2:** crea un token nuevo, actualiza `.env` y revoca el anterior en
  Cloudflare.
