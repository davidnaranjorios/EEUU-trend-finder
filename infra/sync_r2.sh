#!/usr/bin/env bash
# Sincroniza data/processed, data/exports y data/logs a Cloudflare R2 (ADR 0002).
#
# Las borradas en el destino no se pierden: van a _trash/<fecha>/ dentro del bucket.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
# shellcheck source=infra/r2_env.sh
source infra/r2_env.sh

# Protección: si data/processed está vacío (volumen sin montar, o la copia desde R2 falló),
# un sync borraría R2.
PROCESSED="$(readlink -f data)/processed"
if ! find "$PROCESSED" -name '*.parquet' -print -quit 2>/dev/null | grep -q .; then
  echo "sync_r2: $PROCESSED no tiene Parquet (¿volumen sin montar?): no se sincroniza" >&2
  exit 1
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
for dir in processed exports logs; do
  [[ -d "data/$dir" ]] || continue
  rclone sync "data/$dir" "r2:$BUCKET/$dir" \
    --fast-list --transfers 8 --checksum \
    --exclude '*.tmp' --exclude '.*' \
    --backup-dir "r2:$BUCKET/_trash/$STAMP/$dir"
done
echo "sync_r2: ok ($STAMP)"
