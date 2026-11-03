#!/usr/bin/env bash
# Ingesta de texto por tramos reanudables (ADR 0005): Federal Register y EDGAR.
#
# Un tramo = un periodo (mes del Federal Register, trimestre de EDGAR). Tras cada tramo se
# sincronizan a R2 solo las rutas de texto, para no tocar las tablas de FRED. Si el job se corta
# (límite de 6 h), la próxima corrida sigue donde quedó: el avance está en
# data/processed/ingest_state/text/. Los periodos abiertos se reprocesan una vez por corrida.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
# Periodos por tramo: un trimestre de EDGAR toma ~10-15 min; un mes del Federal Register, ~20 s.
EDGAR_PER_TRAMO="${EDGAR_PER_TRAMO:-2}"
FR_PER_TRAMO="${FR_PER_TRAMO:-12}"
read -r -a SOURCES <<<"${TEXT_SOURCES:-federal_register edgar}"
STATE="data/processed/ingest_state"
EXIT_PENDING=10
mkdir -p "$STATE/text"
exec 9>"$STATE/.lock"
flock -n 9 || { echo "otra ejecución tiene el lock" >&2; exit 1; }

TEXT_PATHS=(
  processed/text_docs
  processed/term_freq
  processed/lineage/text_ingest
  processed/ingest_state/text
  logs
)
py() { uv run --quiet python "$@"; }
sync_r2() { infra/sync_r2.sh "${TEXT_PATHS[@]}"; }

tramos() {
  local n="$FR_PER_TRAMO"
  [[ "$1" == edgar ]] && n="$EDGAR_PER_TRAMO"
  while true; do
    set +e
    py -m src.ingest.text "$1" --max-periods "$n"
    rc=$?
    set -e
    sync_r2
    [[ $rc -eq 0 ]] && return 0
    [[ $rc -eq $EXIT_PENDING ]] || return "$rc"
  done
}

for src in "${SOURCES[@]}"; do
  tramos "$src"
done
echo "run_texto: ok"
