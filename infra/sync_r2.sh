#!/usr/bin/env bash
# Sincroniza data/processed y data/exports a Cloudflare R2 (ADR 0002). Ver docs/infra/vps.md.
#
# rclone se configura solo con variables de entorno leídas de .env (sin archivo con secretos).
# Las borradas en el destino no se pierden: van a _trash/<fecha>/ dentro del bucket.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
set -a
# shellcheck disable=SC1091
source .env
set +a
: "${CLOUDFLARE_ACCOUNT_ID:?}" "${AWS_ACCESS_KEY_ID:?}" "${AWS_SECRET_ACCESS_KEY:?}"
BUCKET="${R2_BUCKET:-$(uv run --quiet python -c 'from src.config import load_config; print(load_config()["storage"]["bucket"])')}"

export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true

# Protección: si el volumen no está montado, data/processed estaría vacío y un sync borraría R2.
PROCESSED="$(readlink -f data)/processed"
if ! find "$PROCESSED" -name '*.parquet' -print -quit 2>/dev/null | grep -q .; then
  echo "sync_r2: $PROCESSED no tiene Parquet (¿volumen sin montar?): no se sincroniza" >&2
  exit 1
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
for dir in processed exports; do
  [[ -d "data/$dir" ]] || continue
  rclone sync "data/$dir" "r2:$BUCKET/$dir" \
    --fast-list --transfers 8 --checksum \
    --exclude '*.tmp' --exclude '.*' \
    --backup-dir "r2:$BUCKET/_trash/$STAMP/$dir"
done
echo "sync_r2: ok ($STAMP)"
