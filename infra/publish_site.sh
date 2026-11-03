#!/usr/bin/env bash
# Publica el sitio ya construido (y verificado sin copyright) en el destino elegido (ADR 0009).
#
# Uso: infra/publish_site.sh <destino> <directorio>
#   cloudflare_pages  wrangler (versión fijada) -> proyecto $CF_PAGES_PROJECT; necesita
#                     CLOUDFLARE_API_TOKEN (permiso "Cloudflare Pages: Edit") y
#                     CLOUDFLARE_ACCOUNT_ID en el entorno.
#   none              no hace nada.
# GitHub Pages no pasa por aquí: lo publica el job `pages` de .github/workflows/reporte.yml.
set -euo pipefail
WRANGLER_VERSION="4.151.0"
target="${1:?destino}"
site="${2:?directorio del sitio}"
[[ -f "$site/index.html" ]] || { echo "publish_site: $site/index.html no existe" >&2; exit 1; }
case "$target" in
  none | "")
    echo "publish_site: sin destino; el sitio queda solo como artefacto"
    ;;
  cloudflare_pages)
    for v in CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID CF_PAGES_PROJECT; do
      [[ -n "${!v:-}" ]] || { echo "publish_site: falta $v" >&2; exit 1; }
    done
    npx --yes "wrangler@${WRANGLER_VERSION}" pages deploy "$site" \
      --project-name "$CF_PAGES_PROJECT" --branch main --commit-dirty=true
    ;;
  *)
    echo "publish_site: destino desconocido: $target" >&2
    exit 2
    ;;
esac
