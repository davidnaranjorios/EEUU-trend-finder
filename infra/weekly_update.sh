#!/usr/bin/env bash
# Actualización incremental semanal (ADR 0003). La lanza trend-weekly-update.timer.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
STATE="data/processed/ingest_state"
exec 9>"$STATE/.lock"
flock -n 9 || { echo "otra ejecución tiene el lock" >&2; exit 1; }
py() { uv run --quiet python "$@"; }

# Catálogo (series/updates, o crawl si toca) + valores vigentes de las series cambiadas.
py -m src.ingest.fred update || [[ $? -eq 10 ]]
py -m src.ingest.fred crawl-meta --if-stale
py -m src.eligibility run >/dev/null
# Series que entraron al universo con el embudo nuevo, y vintages de la muestra.
py -m src.ingest.fred observations || [[ $? -eq 10 ]]
py -m src.ingest.fred vintages || [[ $? -eq 10 ]]
py -m src.export >/dev/null
infra/sync_r2.sh
