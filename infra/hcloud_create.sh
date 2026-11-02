#!/usr/bin/env bash
# Crea (o reutiliza) en Hetzner Cloud la llave SSH, el firewall, el volumen y el servidor
# del ADR 0003, vía la API (https://docs.hetzner.cloud). Idempotente: busca cada recurso por
# nombre antes de crearlo. Solo necesita curl y python3; no usa SSH.
#
#   HCLOUD_TOKEN=... SSH_PUBKEY_FILE=~/.ssh/eeuu_trend.pub bash infra/hcloud_create.sh
#
# El token es de un proyecto de Hetzner con permiso Read & Write. Al final imprime la IP y el
# ID del volumen para infra/provision.sh.
set -euo pipefail

: "${HCLOUD_TOKEN:?token de API del proyecto de Hetzner (Read & Write)}"
NAME="${NAME:-eeuu-trend}"
LOCATION="${LOCATION:-nbg1}"
SERVER_TYPE="${SERVER_TYPE:-cpx32}"
IMAGE="${IMAGE:-ubuntu-24.04}"
VOLUME_GB="${VOLUME_GB:-100}"
API="${HCLOUD_API:-https://api.hetzner.cloud/v1}"

log() { printf '[hcloud] %s\n' "$*" >&2; }
api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS --fail-with-body -X "$method" -H "Authorization: Bearer $HCLOUD_TOKEN")
  [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" --data "$body")
  curl "${args[@]}" "$API$path"
}
# Extrae un campo de un JSON por stdin: json 'expresión sobre d' (vacío si no existe).
json() { python3 -c "import json,sys; d=json.load(sys.stdin)
try: v=$1
except (KeyError, IndexError, TypeError): v=''
print('' if v is None else v)"; }

if [[ -n "${SSH_PUBKEY_FILE:-}" ]]; then
  SSH_PUBKEY="$(cat "$SSH_PUBKEY_FILE")"
fi
: "${SSH_PUBKEY:?llave pública SSH (SSH_PUBKEY o SSH_PUBKEY_FILE)}"
[[ "$SSH_PUBKEY" == ssh-* ]] || { echo "SSH_PUBKEY no parece una llave pública" >&2; exit 1; }

log "tipo de servidor $SERVER_TYPE"
[[ -n "$(api GET "/server_types?name=$SERVER_TYPE" | json "d['server_types'][0]['id']")" ]] \
  || { echo "Hetzner no ofrece el tipo $SERVER_TYPE" >&2; exit 1; }

log "llave SSH"
KEY_ID="$(api GET "/ssh_keys?name=$NAME" | json "d['ssh_keys'][0]['id']")"
if [[ -z "$KEY_ID" ]]; then
  body="$(python3 -c 'import json,sys; print(json.dumps({"name": sys.argv[1], "public_key": sys.argv[2]}))' "$NAME" "$SSH_PUBKEY")"
  KEY_ID="$(api POST /ssh_keys "$body" | json "d['ssh_key']['id']")"
fi

log "firewall (solo SSH entrante)"
FW_ID="$(api GET "/firewalls?name=$NAME" | json "d['firewalls'][0]['id']")"
if [[ -z "$FW_ID" ]]; then
  FW_ID="$(api POST /firewalls "{\"name\":\"$NAME\",\"rules\":[{\"direction\":\"in\",\"protocol\":\"tcp\",\"port\":\"22\",\"source_ips\":[\"0.0.0.0/0\",\"::/0\"]}]}" \
    | json "d['firewall']['id']")"
fi

log "volumen de ${VOLUME_GB} GB en $LOCATION"
VOL_JSON="$(api GET "/volumes?name=$NAME-data")"
VOL_ID="$(json "d['volumes'][0]['id']" <<<"$VOL_JSON")"
if [[ -z "$VOL_ID" ]]; then
  VOL_ID="$(api POST /volumes "{\"name\":\"$NAME-data\",\"size\":$VOLUME_GB,\"location\":\"$LOCATION\",\"automount\":false}" \
    | json "d['volume']['id']")"
else
  VOL_LOC="$(json "d['volumes'][0]['location']['name']" <<<"$VOL_JSON")"
  [[ "$VOL_LOC" == "$LOCATION" ]] || { echo "el volumen existe en $VOL_LOC, no en $LOCATION" >&2; exit 1; }
fi

log "servidor $NAME ($SERVER_TYPE, $IMAGE)"
SRV_ID="$(api GET "/servers?name=$NAME" | json "d['servers'][0]['id']")"
if [[ -z "$SRV_ID" ]]; then
  SRV_ID="$(api POST /servers "{\"name\":\"$NAME\",\"server_type\":\"$SERVER_TYPE\",\"image\":\"$IMAGE\",\"location\":\"$LOCATION\",\"ssh_keys\":[$KEY_ID],\"firewalls\":[{\"firewall\":$FW_ID}],\"volumes\":[$VOL_ID],\"automount\":false,\"start_after_create\":true}" \
    | json "d['server']['id']")"
fi

for _ in $(seq 60); do
  SRV="$(api GET "/servers/$SRV_ID")"
  [[ "$(json "d['server']['status']" <<<"$SRV")" == running ]] && break
  sleep "${POLL_S:-5}"
done
IP="$(json "d['server']['public_net']['ipv4']['ip']" <<<"$SRV")"
ATTACHED="$(api GET "/volumes/$VOL_ID" | json "d['volume']['server']")"
[[ "$ATTACHED" == "$SRV_ID" ]] || api POST "/volumes/$VOL_ID/actions/attach" "{\"server\":$SRV_ID,\"automount\":false}" >/dev/null

log "listo"
printf 'SERVER_ID=%s\nIP=%s\nVOLUME_ID=%s\n' "$SRV_ID" "$IP" "$VOL_ID"
