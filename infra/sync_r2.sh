#!/usr/bin/env bash
# Sincroniza data/processed, data/signals, data/exports y data/logs a Cloudflare R2 (ADR 0002).
# Con argumentos, solo esas rutas bajo data/ (p. ej. `sync_r2.sh processed/term_freq logs`):
# así la ingesta de texto no toca las tablas de FRED (ADR 0005).
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
if [[ $# -eq 0 ]]; then set -- processed signals exports logs; fi
for dir in "$@"; do
  [[ -d "data/$dir" ]] || continue
  # Una tabla local sin Parquet borraría su copia en R2: se salta (el estado es JSON).
  case "$dir" in
    processed/ingest_state*) ;;
    processed/*)
      if ! find "data/$dir" -name '*.parquet' -print -quit | grep -q .; then
        echo "sync_r2: data/$dir sin Parquet: no se sincroniza" >&2
        continue
      fi
      ;;
  esac
  # logs/reporte/ es del workflow Reporte (otro grupo de concurrencia, ADR 0009): una sync de
  # logs/ entero no lo toca (rclone no borra en el destino lo excluido sin --delete-excluded).
  extra=()
  if [[ "$dir" == "logs" ]]; then extra=(--exclude 'reporte/**'); fi
  rclone sync "data/$dir" "r2:$BUCKET/$dir" \
    --fast-list --transfers 8 --checksum --no-update-modtime \
    --exclude '*.tmp' --exclude '.*' "${extra[@]}" \
    --backup-dir "r2:$BUCKET/_trash/$STAMP/$dir"
  # R2 no implementa la autocopia con que rclone corrige la fecha de modificación (501
  # NotImplemented); --no-update-modtime la evita. Esta comprobación no depende de eso.
  rclone check "data/$dir" "r2:$BUCKET/$dir" --one-way --checksum --fast-list \
    --exclude '*.tmp' --exclude '.*' "${extra[@]}"
done
echo "sync_r2: ok ($STAMP)"
