#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "Install uv first." >&2; exit 1; }
uv tool install graphifyy
command -v graphify >/dev/null || { echo "Graphify not in PATH; run uv tool update-shell and restart your shell." >&2; exit 1; }
graphify install --project
graphify install --project --platform codex
graphify extract . --code-only
printf '%s\n' "Graphify prepared. Try: graphify query 'How do ingestion and ranking connect?'"
printf '%s\n' "Review generated assistant skills before committing."
