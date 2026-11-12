# Ejecutar desde la raíz del repo, o desde cualquier ruta si se conserva este script.
$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv no está instalado o no está en PATH. Instala uv y vuelve a ejecutar."
}
uv tool install graphifyy
if ($LASTEXITCODE -ne 0) { throw "No se pudo instalar graphifyy." }
$graphify = Get-Command graphify -ErrorAction SilentlyContinue
if (-not $graphify) {
  throw "Graphify no aparece en PATH. Ejecuta 'uv tool update-shell', reinicia PowerShell y repite."
}
graphify install --project
if ($LASTEXITCODE -ne 0) { throw "Falló la integración de Claude Code." }
graphify install --project --platform codex
if ($LASTEXITCODE -ne 0) { throw "Falló la integración de Codex." }
graphify extract . --code-only
if ($LASTEXITCODE -ne 0) { throw "Falló la indexación del código." }
Write-Host "Graphify preparado. Prueba: graphify query 'How do ingestion and ranking connect?'"
Write-Host "Revisa las skills generadas antes de agregarlas a Git."
