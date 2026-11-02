#!/usr/bin/env bash
# Crea (o reutiliza) el bucket de R2 del ADR 0002 vía la API de Cloudflare. Idempotente.
#
#   CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=... bash infra/r2_create_bucket.sh
#
# El token necesita el permiso "Workers R2 Storage: Edit" sobre la cuenta. Las llaves S3 que
# usa el VPS (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) son otras: se crean en el panel de
# R2 con permiso solo sobre este bucket (docs/infra/vps.md, paso 3).
set -euo pipefail
: "${CLOUDFLARE_API_TOKEN:?token de API de Cloudflare con Workers R2 Storage: Edit}"
: "${CLOUDFLARE_ACCOUNT_ID:?ID de la cuenta de Cloudflare}"
BUCKET="${BUCKET:-eeuu-trend-finder}"
API="${CLOUDFLARE_API:-https://api.cloudflare.com/client/v4}/accounts/$CLOUDFLARE_ACCOUNT_ID/r2/buckets"
auth=(-H "Authorization: Bearer $CLOUDFLARE_API_TOKEN")

code="$(curl -sS -o /dev/null -w '%{http_code}' "${auth[@]}" "$API/$BUCKET")"
if [[ "$code" == 200 ]]; then
  echo "[r2] el bucket $BUCKET ya existe"
elif [[ "$code" == 404 ]]; then
  curl -sS --fail-with-body "${auth[@]}" -H "Content-Type: application/json" \
    --data "{\"name\":\"$BUCKET\"}" "$API" >/dev/null
  echo "[r2] bucket $BUCKET creado"
else
  echo "[r2] respuesta inesperada de Cloudflare ($code): revisa el token y el ID de cuenta" >&2
  exit 1
fi
