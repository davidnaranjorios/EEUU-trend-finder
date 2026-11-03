#!/usr/bin/env bash
# Sincroniza data/processed, data/exports y data/logs a Cloudflare R2 (ADR 0002).
#
# Antes de subir compacta los Parquet (un archivo por bucket, ADR 0004): así R2 no acumula
# miles de archivos chicos que DuckDB/httpfs tendría que abrir uno por uno.
# Las borradas en el destino no se pierden: van a _trash/<fecha>/ dentro del bucket, que una
# regla de ciclo de vida de R2 borra a los 30 días (docs/infra/github-actions.md).
# Al final verifica que todo lo local esté en R2 con el mismo checksum; si no, falla.
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
uv run --quiet python -m src.ingest.store compact
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
for dir in processed exports logs; do
  [[ -d "data/$dir" ]] || continue
  rclone sync "data/$dir" "r2:$BUCKET/$dir" \
    --fast-list --transfers 8 --checksum --no-update-modtime \
    --exclude '*.tmp' --exclude '.*' \
    --backup-dir "r2:$BUCKET/_trash/$STAMP/$dir"
  # R2 no implementa la autocopia con que rclone corrige la fecha de modificación (501
  # NotImplemented); --no-update-modtime la evita. Esta comprobación no depende de eso.
  rclone check "data/$dir" "r2:$BUCKET/$dir" --one-way --checksum --fast-list \
    --exclude '*.tmp' --exclude '.*'
done
echo "sync_r2: ok ($STAMP)"
