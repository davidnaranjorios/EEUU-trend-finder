#!/usr/bin/env bash
# Instala una versión fija de rclone verificando su SHA-256 (ADR 0004).
#
# El rclone de apt en Ubuntu 24.04 es la 1.60, que choca con R2 (501 NotImplemented en cada
# primer intento de sync). Esta versión y su checksum salen de downloads.rclone.org/<v>/SHA256SUMS.
set -euo pipefail
VERSION="${RCLONE_VERSION:-v1.68.2}"
SHA256="${RCLONE_SHA256:-0e6fa18051e67fc600d803a2dcb10ddedb092247fc6eee61be97f64ec080a13c}"
DEST="${RCLONE_DEST:-/usr/local/bin}"
name="rclone-${VERSION}-linux-amd64"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/$name.zip" "https://downloads.rclone.org/${VERSION}/${name}.zip"
echo "$SHA256  $tmp/$name.zip" | sha256sum -c --quiet
unzip -q "$tmp/$name.zip" -d "$tmp"
install -m 0755 "$tmp/$name/rclone" "$DEST/rclone"
"$DEST/rclone" version | sed -n 1p  # sed lee todo: con head, pipefail daba 141 (SIGPIPE)
