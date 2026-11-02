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
# Limpieza tolerante: al pegar un secret es fácil arrastrar espacios, saltos de línea o la URL
# completa del endpoint (https://<id>.r2.cloudflarestorage.com). Sin esto la dirección queda
# mal formada y la conexión falla con un "tls: handshake failure" poco explicativo.
strip() { local v="${1//[[:space:]]/}"; printf '%s' "$v"; }
ACCOUNT="$(strip "$CLOUDFLARE_ACCOUNT_ID")"
ACCOUNT="${ACCOUNT#https://}"
ACCOUNT="${ACCOUNT#http://}"
ACCOUNT="${ACCOUNT%%/*}"
ACCOUNT="${ACCOUNT%%.*}"
ACCOUNT="${ACCOUNT,,}"
AWS_ACCESS_KEY_ID="$(strip "$AWS_ACCESS_KEY_ID")"
AWS_SECRET_ACCESS_KEY="$(strip "$AWS_SECRET_ACCESS_KEY")"
# Se informa solo la forma (largo), nunca el valor.
if [[ ! "$ACCOUNT" =~ ^[0-9a-f]{32}$ ]]; then
  echo "CLOUDFLARE_ACCOUNT_ID no es un Account ID de Cloudflare: se esperan 32 caracteres" \
    "hexadecimales y, tras limpiar, quedan ${#ACCOUNT}. Revisa el secret." >&2
  exit 1
fi
if [[ ${#AWS_ACCESS_KEY_ID} -ne 32 || ${#AWS_SECRET_ACCESS_KEY} -ne 64 ]]; then
  echo "credencial S3 de R2 con forma inesperada: Access Key ID tiene ${#AWS_ACCESS_KEY_ID}" \
    "caracteres (se esperan 32) y Secret Access Key ${#AWS_SECRET_ACCESS_KEY} (se esperan 64)." \
    "¿Se pegó el 'Token value' en lugar de las llaves S3?" >&2
  exit 1
fi
# Buckets creados con jurisdicción (p. ej. UE) usan otro endpoint: R2_JURISDICTION=eu.
HOST="${ACCOUNT}.${R2_JURISDICTION:+${R2_JURISDICTION,,}.}r2.cloudflarestorage.com"
BUCKET="${R2_BUCKET:-$(uv run --quiet python -c 'from src.config import load_config; print(load_config()["storage"]["bucket"])')}"
export BUCKET
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="https://${HOST}"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
