#!/usr/bin/env bash
# Aprovisionamiento idempotente del VPS (Ubuntu 24.04). Ver docs/infra/vps.md y ADR 0003.
#
# Se corre como root, una vez o las que haga falta: cada paso verifica antes de cambiar.
#   sudo SSH_PUBKEY_FILE=/root/.ssh/authorized_keys VOLUME_ID=12345678 \
#        REPO_URL=https://github.com/<owner>/<repo>.git BRANCH=main bash infra/provision.sh
#
# Crea dos usuarios: ADMIN_USER (por defecto "ops"), con sudo y acceso solo por llave SSH, y
# APP_USER ("trend"), sin root y sin login SSH, dueño del código, los datos y los servicios.
set -euo pipefail

: "${SSH_PUBKEY_FILE:?ruta a la llave pública SSH del administrador}"
: "${VOLUME_ID:?ID del volumen de Hetzner (consola > Volumes)}"
: "${REPO_URL:?URL del repositorio}"
BRANCH="${BRANCH:-main}"
ADMIN_USER="${ADMIN_USER:-ops}"
APP_USER="${APP_USER:-trend}"
MOUNT="${MOUNT:-/mnt/data}"
UV_VERSION="${UV_VERSION:-0.11.32}"
APP_DIR="/home/${APP_USER}/app"
DATA_DIR="${MOUNT}/eeuu-trend-finder/data"
DEVICE="/dev/disk/by-id/scsi-0HC_Volume_${VOLUME_ID}"

log() { printf '[provision] %s\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "correr como root" >&2; exit 1; }
[[ -s "$SSH_PUBKEY_FILE" ]] || { echo "no existe o está vacía: $SSH_PUBKEY_FILE" >&2; exit 1; }

log "paquetes"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates ufw unattended-upgrades fail2ban rclone >/dev/null

log "usuarios"
id "$ADMIN_USER" &>/dev/null || useradd --create-home --shell /bin/bash "$ADMIN_USER"
usermod -aG sudo "$ADMIN_USER"
# sudo sin contraseña para el admin: no hay contraseñas en este servidor (solo llaves).
echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" >"/etc/sudoers.d/90-$ADMIN_USER"
chmod 440 "/etc/sudoers.d/90-$ADMIN_USER"
visudo -cq
id "$APP_USER" &>/dev/null || useradd --create-home --shell /bin/bash "$APP_USER"
passwd -l "$APP_USER" >/dev/null
passwd -l "$ADMIN_USER" >/dev/null

log "llave SSH del administrador"
install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "/home/$ADMIN_USER/.ssh"
AUTH="/home/$ADMIN_USER/.ssh/authorized_keys"
touch "$AUTH"
while IFS= read -r key; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  grep -qxF "$key" "$AUTH" || echo "$key" >>"$AUTH"
done <"$SSH_PUBKEY_FILE"
chown "$ADMIN_USER:$ADMIN_USER" "$AUTH"
chmod 600 "$AUTH"

log "sshd: solo llave, sin root, solo $ADMIN_USER"
cat >/etc/ssh/sshd_config.d/99-eeuu-hardening.conf <<CONF
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers $ADMIN_USER
CONF
sshd -t  # no recargar una configuración inválida
systemctl reload ssh

log "firewall: solo SSH entrante"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null

log "actualizaciones de seguridad automáticas"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF

log "volumen $DEVICE -> $MOUNT"
[[ -b "$DEVICE" ]] || { echo "no existe el volumen $DEVICE (¿adjunto al servidor?)" >&2; exit 1; }
if ! blkid -o value -s TYPE "$DEVICE" >/dev/null 2>&1; then
  log "volumen sin sistema de archivos: formateando ext4"
  mkfs.ext4 -q -L eeuu-data "$DEVICE"
fi
UUID="$(blkid -o value -s UUID "$DEVICE")"
install -d "$MOUNT"
grep -q "UUID=$UUID" /etc/fstab || echo "UUID=$UUID $MOUNT ext4 defaults,nofail,discard 0 2" >>/etc/fstab
mountpoint -q "$MOUNT" || mount "$MOUNT"
install -d -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"

log "uv $UV_VERSION para $APP_USER"
if ! sudo -u "$APP_USER" -H bash -lc "/home/$APP_USER/.local/bin/uv --version 2>/dev/null | grep -q '$UV_VERSION'"; then
  sudo -u "$APP_USER" -H bash -lc "curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh"
fi

log "código en $APP_DIR ($BRANCH)"
if [[ -d "$APP_DIR/.git" ]]; then
  sudo -u "$APP_USER" -H git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  sudo -u "$APP_USER" -H git -C "$APP_DIR" checkout --quiet "$BRANCH"
  sudo -u "$APP_USER" -H git -C "$APP_DIR" merge --ff-only --quiet "origin/$BRANCH"
else
  sudo -u "$APP_USER" -H git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
if [[ -d "$APP_DIR/data" && ! -L "$APP_DIR/data" ]]; then
  echo "$APP_DIR/data existe y no es un enlace: muévelo a $DATA_DIR a mano" >&2
  exit 1
fi
ln -sfn "$DATA_DIR" "$APP_DIR/data"

log ".env con permisos 600"
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 -o "$APP_USER" -g "$APP_USER" "$APP_DIR/.env.example" "$ENV_FILE"
  log "  creado desde .env.example: rellénalo antes de habilitar los servicios"
fi
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

log "dependencias (uv sync --locked)"
sudo -u "$APP_USER" -H bash -lc "cd '$APP_DIR' && /home/$APP_USER/.local/bin/uv sync --locked --quiet"

log "unidades systemd"
for unit in "$APP_DIR"/infra/systemd/*.service "$APP_DIR"/infra/systemd/*.timer; do
  install -m 644 "$unit" /etc/systemd/system/
done
systemctl daemon-reload
log "listo. Siguiente paso: rellenar $ENV_FILE y seguir docs/infra/vps.md (paso 6)."
