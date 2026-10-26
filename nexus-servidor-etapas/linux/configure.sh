#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
usage='Uso: configure.sh --bind-ip IPv4_LAN [--port PORTA] [--dry-run]'
ip=''; port=8001; mode=''
while (($#)); do
  case "$1" in
    --bind-ip) ip="${2:-}"; shift 2 ;;
    --port) port="${2:-}"; shift 2 ;;
    --dry-run) mode=--dry-run; shift ;;
    --help) echo "$usage"; exit 0 ;;
    *) die "$usage" ;;
  esac
done
[[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ && "$ip" != 0.0.0.0 && "$ip" != 127.* ]] || die 'Informe IPv4 LAN específico.'
IFS=. read -r a b c d <<<"$ip"
for octet in "$a" "$b" "$c" "$d"; do (( octet <= 255 )) || die 'IPv4 inválido.'; done
[[ "$port" =~ ^[0-9]{1,5}$ ]] || die 'Porta inválida.'
((10#$port >= 1024 && 10#$port <= 65535)) || die 'Use porta entre 1024 e 65535.'
[[ ! -e "$NEXUS_ROOT/.env" ]] || die '.env já existe; nenhuma credencial será substituída.'
if [[ "$mode" == --dry-run ]]; then
  echo "DRY-RUN: criaria .env com credenciais aleatórias, IP=$ip e porta=$port (modo 0600)"
  exit 0
fi
require_linux
command -v od >/dev/null 2>&1 || die 'od ausente.'
umask 077
tmp="$(mktemp "$NEXUS_ROOT/.env.tmp.XXXXXX")"
trap 'rm -f -- "$tmp"' EXIT
dbpass="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
apikey="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
cat >"$tmp" <<EOF
POSTGRES_PASSWORD=$dbpass
ADMIN_API_KEY=$apikey
NEXUS_BIND_IP=$ip
NEXUS_HOST_PORT=$port
ALLOW_SOURCE_DOWNLOAD=true
I9_UPDATES_PAUSED=true
I9_CATALOG_AUTO_REFRESH=false
I9_ON_DEMAND_WORKER=true
I9_CHECK_ON_START=true
I9_SCHEDULER_ENABLED=false
I9_MAX_DOWNLOAD_BYTES=500000000
CORS_ORIGINS=http://$ip:$port
EOF
chmod 600 "$tmp"
mv -n -- "$tmp" "$NEXUS_ROOT/.env"
[[ -f "$tmp" ]] && die 'Não foi possível criar .env sem sobrescrever arquivo existente.'
trap - EXIT
echo 'Configuração criada. Guarde as credenciais com segurança e configure o administrador após instalar.'
