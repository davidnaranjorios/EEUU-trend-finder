#!/usr/bin/env bash
# Configuración de rclone para R2, compartida por sync_r2.sh y pull_r2.sh (se usa con `source`).
#
# Credenciales desde el entorno: en el VPS vienen de .env; en GitHub Actions, de los secrets
# del repositorio. Nunca se escriben en un archivo de configuración de rclone.
set -euo pipefail
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
: "${CLOUDFLARE_ACCOUNT_ID:?falta CLOUDFLARE_ACCOUNT_ID}"
: "${AWS_ACCESS_KEY_ID:?falta AWS_ACCESS_KEY_ID}"
: "${AWS_SECRET_ACCESS_KEY:?falta AWS_SECRET_ACCESS_KEY}"
BUCKET="${R2_BUCKET:-$(uv run --quiet python -c 'from src.config import load_config; print(load_config()["storage"]["bucket"])')}"
export BUCKET
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
