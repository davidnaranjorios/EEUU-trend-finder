#!/usr/bin/env bash
# Trae desde R2 el estado y los logs de la corrida anterior a data/ (ADR 0004).
#
# En GitHub Actions cada corrida empieza en una máquina vacía: sin este paso se volvería a
# descargar todo y, peor, el sync final no tendría con qué comparar. Copia (no borra nada local).
# Con argumentos, solo esas rutas bajo data/ (p. ej. las tablas de texto, ADR 0005).
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
# shellcheck source=infra/r2_env.sh
source infra/r2_env.sh
if [[ $# -eq 0 ]]; then set -- processed signals logs; fi
for dir in "$@"; do
  mkdir -p "data/$dir"
  rc=0
  rclone copy "r2:$BUCKET/$dir" "data/$dir" --fast-list --transfers 16 --checksum || rc=$?
  # 3 = el directorio no existe en R2 (primera corrida de una tabla nueva): no es un error.
  if [[ $rc -eq 3 ]]; then
    echo "pull_r2: r2:$BUCKET/$dir no existe todavía" >&2
  elif [[ $rc -ne 0 ]]; then
    exit "$rc"
  fi
done
n="$(find data/processed -name '*.parquet' | wc -l)"
echo "pull_r2: $n archivos Parquet desde r2:$BUCKET/processed"
