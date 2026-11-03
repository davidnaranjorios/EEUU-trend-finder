#!/usr/bin/env bash
# Descarga inicial por tramos, reanudable (ADR 0003). La lanza trend-initial-download.service.
#
# Etapas: catálogo -> metadatos -> embudo (una vez) -> valores vigentes vía v2 (un tramo = N
# releases) -> vintages de la muestra (un tramo = N series) -> exportes. Al cerrar cada tramo
# sincroniza a R2. Si se corta, la siguiente ejecución sigue donde quedó: el progreso está en
# data/processed/ingest_state/. Solo baja los grupos de `download.groups` (config.yaml).
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
RELEASES_PER_TRAMO="${RELEASES_PER_TRAMO:-10}"
SERIES_PER_TRAMO="${SERIES_PER_TRAMO:-200}"
STATE="data/processed/ingest_state"
EXIT_PENDING=10
mkdir -p "$STATE"
exec 9>"$STATE/.lock"
flock -n 9 || { echo "otra ejecución tiene el lock" >&2; exit 1; }

py() { uv run --quiet python "$@"; }
sync_r2() { infra/sync_r2.sh; }

# Corre un comando acotado hasta que no deje pendientes, sincronizando tras cada tramo.
tramos() {
  while true; do
    set +e
    py -m src.ingest.fred "$@"
    rc=$?
    set -e
    sync_r2
    [[ $rc -eq 0 ]] && return 0
    [[ $rc -eq $EXIT_PENDING ]] || return "$rc"
  done
}

py -m src.ingest.fred crawl --if-stale
py -m src.ingest.fred crawl-meta --if-stale
# El embudo se corre una sola vez por descarga inicial, para que todos los tramos bajen el
# mismo universo. Para recalcularlo (p. ej., tras cambiar umbrales), borrar este marcador.
if [[ ! -f "$STATE/initial_funnel.done" ]]; then
  py -m src.eligibility run >"$STATE/initial_funnel.json"
  touch "$STATE/initial_funnel.done"
fi
sync_r2
tramos download --max-releases "$RELEASES_PER_TRAMO"
tramos vintages --max-series "$SERIES_PER_TRAMO"
export_rc=0
py -m src.export >/dev/null || export_rc=$?
# El marcador va ANTES del último sync: si se crea después, nunca llega a R2 y en GitHub
# Actions (máquina nueva en cada corrida) toda corrida vuelve a ser "descarga inicial", que
# salta el embudo y nunca corre la actualización semanal.
touch "$STATE/initial_download.done"
sync_r2
[[ $export_rc -eq 0 ]] || { echo "export falló (rc=$export_rc)" >&2; exit "$export_rc"; }
echo "descarga inicial completa"
