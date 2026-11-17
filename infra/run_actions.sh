#!/usr/bin/env bash
# Punto de entrada del workflow de GitHub Actions (ADR 0004).
#
# Mientras la descarga inicial no haya terminado, la continúa (run_tramos.sh es reanudable y
# sincroniza a R2 tras cada tramo); después, corre la actualización semanal. Solo FRED: las
# fuentes de agencias y de mercado corren en sus propios workflows (ADR 0027), cada uno con su
# presupuesto y su sincronización a R2.
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
