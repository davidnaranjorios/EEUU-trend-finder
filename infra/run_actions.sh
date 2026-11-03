#!/usr/bin/env bash
# Punto de entrada del workflow de GitHub Actions (ADR 0004).
#
# Mientras la descarga inicial no haya terminado, la continúa (run_tramos.sh es reanudable y
# sincroniza a R2 tras cada tramo); después, corre la actualización semanal. Luego, las fuentes
# de la Fase 4 (BLS, BEA, Census), que llevan su propio estado y no dependen de FRED.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
if [[ -f data/processed/ingest_state/initial_download.done ]]; then
  echo "run_actions: actualización semanal"
  infra/weekly_update.sh
else
  echo "run_actions: descarga inicial (o su continuación)"
  infra/run_tramos.sh
fi
infra/sources_update.sh
