#!/usr/bin/env bash
# Genera graphify-out/graph.json (AST determinista, sin IA ni claves; ~10 s).
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "Install uv first." >&2; exit 1; }
uv tool install graphifyy==0.9.107 >/dev/null
export PATH="$HOME/.local/bin:$PATH"
graphify extract . --code-only
printf '%s\n' "Listo. Consulta: graphify query 'dónde se calcula el efecto de tasas'"
