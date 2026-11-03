#!/usr/bin/env bash
# Fuentes de la Fase 4 (BLS, BEA, Census; ADR 0007): catálogo incremental + valores vigentes.
#
# Corre después de FRED en el mismo job (run_actions.sh). Cada fuente es independiente: si una
# falla, las demás siguen y el script termina en error al final. `update` es reanudable (estado
# por unidad) y respeta un presupuesto de minutos por fuente; lo pendiente (código 10) sigue en
# la corrida siguiente, igual que la descarga inicial. Tras cada fuente se sincronizan solo sus
# rutas, así nunca se toca lo de FRED ni lo de texto.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
STATE="data/processed/ingest_state"
mkdir -p "$STATE"
exec 9>"$STATE/.sources.lock"
flock -n 9 || { echo "sources_update: otra ejecución tiene el lock" >&2; exit 1; }
py() { uv run --quiet python "$@"; }

BUDGET_MIN="${SOURCES_BUDGET_MIN:-60}"
if [[ $# -eq 0 ]]; then set -- bls bea census; fi
failed=()
for src in "$@"; do
  echo "sources_update: $src (presupuesto ${BUDGET_MIN} min)"
  rc=0
  py -m src.ingest."$src" update --budget-min "$BUDGET_MIN" >/dev/null || rc=$?
  if [[ $rc -ne 0 && $rc -ne 10 ]]; then
    echo "sources_update: $src falló (rc=$rc)" >&2
    failed+=("$src")
  fi
  # Lo hecho (aunque falte una parte) se sube igual: el estado por unidad lo hace reanudable.
  infra/sync_r2.sh \
    "processed/series/$src" "processed/series_meta/$src" \
    "processed/lineage/eligibility/$src" \
    "processed/_internal/source_catalog/$src" "processed/_internal/source_unit_state/$src" \
    processed/ingest_state logs
done
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "sources_update: fallaron ${failed[*]}" >&2
  exit 1
fi
