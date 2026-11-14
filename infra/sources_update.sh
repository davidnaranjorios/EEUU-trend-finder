#!/usr/bin/env bash
# Fuentes de la Fase 4 (BLS, BEA, Census; ADR 0007 y 0019): catálogo incremental + valores vigentes.
#
# Corre después de FRED en el mismo job (run_actions.sh). Cada fuente es independiente: si una
# falla, se cuelga o se queda sin tiempo, las demás siguen y el script termina en error al final.
# `update` es reanudable (estado por unidad) y respeta un presupuesto de minutos por fuente; lo
# pendiente (código 10) sigue en la corrida siguiente. Tras cada fuente se sincronizan solo sus
# rutas, así nunca se toca lo de FRED ni lo de texto.
#
# Redes de seguridad (ADR 0019), de adentro hacia afuera:
#   1. el presupuesto duro de `src/ingest/common.py` (se mira dentro de cada unidad);
#   2. el watchdog del propio proceso (presupuesto + `hard_grace_min` de config.yaml);
#   3. aquí, `timeout` por fuente = presupuesto + margen (SOURCES_GRACE_MIN, 15 por defecto) y
#      `timeout` de la sincronización (SOURCES_SYNC_TIMEOUT_MIN, 30): una fuente colgada se mata,
#      se sincroniza lo que dejó hecho y se sigue con la siguiente.
#   4. SOURCES_TOTAL_MIN (240) reparte el tiempo del script entre las fuentes: si no queda para
#      un presupuesto útil, esa fuente se salta (queda pendiente para la próxima corrida).
# Para pruebas, SOURCES_BUDGET_S / SOURCES_GRACE_S / SOURCES_SYNC_TIMEOUT_S fijan los tiempos en
# segundos.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
STATE="data/processed/ingest_state"
mkdir -p "$STATE"
exec 9>"$STATE/.sources.lock"
flock -n 9 || { echo "sources_update: otra ejecución tiene el lock" >&2; exit 1; }
py() { uv run --quiet python "$@"; }

BUDGET_S="${SOURCES_BUDGET_S:-$(( ${SOURCES_BUDGET_MIN:-60} * 60 ))}"
GRACE_S="${SOURCES_GRACE_S:-$(( ${SOURCES_GRACE_MIN:-15} * 60 ))}"
SYNC_S="${SOURCES_SYNC_TIMEOUT_S:-$(( ${SOURCES_SYNC_TIMEOUT_MIN:-30} * 60 ))}"
TOTAL_S="${SOURCES_TOTAL_S:-$(( ${SOURCES_TOTAL_MIN:-240} * 60 ))}"
MIN_USEFUL_S="${SOURCES_MIN_USEFUL_S:-300}"
# cbp, bds y econcensus son métricas de mercado por NAICS (ADR 0024): consultas pequeñas y anuales,
# así que tienen un presupuesto propio corto (SOURCES_BUDGET_MARKET_MIN, 20 por defecto).
MARKET_BUDGET_S="${SOURCES_BUDGET_MARKET_S:-$(( ${SOURCES_BUDGET_MARKET_MIN:-20} * 60 ))}"
if [[ $# -eq 0 ]]; then set -- bls bea census cbp bds econcensus; fi
START=$SECONDS
failed=()
skipped=()
for src in "$@"; do
  # Tiempo del script que queda, descontando el margen y la sincronización de esta fuente.
  left=$(( TOTAL_S - (SECONDS - START) - GRACE_S - SYNC_S ))
  budget=$BUDGET_S
  case "$src" in cbp|bds|econcensus) budget=$MARKET_BUDGET_S ;; esac
  if (( left < budget )); then budget=$left; fi
  if (( budget < MIN_USEFUL_S )); then
    echo "sources_update: $src sin tiempo útil (quedan ${left}s del total); queda pendiente" >&2
    skipped+=("$src")
    continue
  fi
  limit=$(( budget + GRACE_S ))
  echo "sources_update: $src (presupuesto ${budget}s, timeout ${limit}s)"
  rc=0
  timeout -k 60 "$limit" \
    uv run --quiet python -m src.ingest."$src" update --budget-min "$(awk "BEGIN{print $budget/60}")" \
    >/dev/null || rc=$?
  if [[ $rc -eq 124 || $rc -eq 137 ]]; then
    echo "sources_update: $src cortada por timeout (rc=$rc); se sincroniza lo hecho" >&2
    failed+=("$src(timeout)")
  elif [[ $rc -eq 11 ]]; then
    echo "sources_update: $src cortada por su watchdog (rc=11); se sincroniza lo hecho" >&2
    failed+=("$src(watchdog)")
  elif [[ $rc -ne 0 && $rc -ne 10 ]]; then
    echo "sources_update: $src falló (rc=$rc)" >&2
    failed+=("$src")
  fi
  # Lo hecho (aunque falte una parte o la fuente se haya cortado) se sube igual: el estado por
  # unidad lo hace reanudable. Un fallo de la sync no impide correr las demás fuentes.
  sync_rc=0
  timeout -k 60 "$SYNC_S" infra/sync_r2.sh \
    "processed/series/$src" "processed/series_meta/$src" \
    "processed/lineage/eligibility/$src" \
    "processed/_internal/source_catalog/$src" "processed/_internal/source_unit_state/$src" \
    processed/ingest_state logs || sync_rc=$?
  if [[ $sync_rc -ne 0 ]]; then
    echo "sources_update: la sincronización de $src falló (rc=$sync_rc)" >&2
    failed+=("$src(sync)")
  fi
done
# Tabla derivada de mercado por NAICS: solo si corrió alguna fuente de mercado.
for src in "$@"; do
  case "$src" in
    cbp|bds|econcensus)
      rc=0
      timeout -k 60 "$MARKET_BUDGET_S" uv run --quiet python -m src.market build >/dev/null || rc=$?
      if [[ $rc -ne 0 ]]; then
        echo "sources_update: src.market build falló (rc=$rc)" >&2
        failed+=("market")
      else
        sync_rc=0
        timeout -k 60 "$SYNC_S" infra/sync_r2.sh processed/market || sync_rc=$?
        [[ $sync_rc -eq 0 ]] || failed+=("market(sync)")
      fi
      break ;;
  esac
done
if [[ ${#skipped[@]} -gt 0 ]]; then
  echo "sources_update: sin tiempo, pendientes para la próxima corrida: ${skipped[*]}" >&2
fi
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "sources_update: fallaron ${failed[*]}" >&2
  exit 1
fi
