#!/usr/bin/env bash
# Trae desde R2 el estado y los logs de la corrida anterior a data/ (ADR 0004).
#
# En GitHub Actions cada corrida empieza en una máquina vacía: sin este paso se volvería a
# descargar todo y, peor, el sync final no tendría con qué comparar. Copia (no borra nada local).
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
# shellcheck source=infra/r2_env.sh
source infra/r2_env.sh
for dir in processed signals logs; do
  mkdir -p "data/$dir"
  rclone copy "r2:$BUCKET/$dir" "data/$dir" --fast-list --transfers 16 --checksum
done
n="$(find data/processed -name '*.parquet' | wc -l)"
echo "pull_r2: $n archivos Parquet desde r2:$BUCKET/processed"
