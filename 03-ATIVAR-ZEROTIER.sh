#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
if [[ ! -t 0 ]]; then
  exec x-terminal-emulator -e bash "$PWD/03-ATIVAR-ZEROTIER.sh"
fi
trap 'echo; read -r -p "Pressione Enter para fechar..." _ || true' EXIT
read -r -p 'Network ID da rede privada ZeroTier (16 caracteres): ' network_id
bash linux/zerotier.sh --network-id "$network_id"
if [[ -f linux/compose.zerotier.yaml && -f .env ]]; then
  source linux/common.sh
  sudo "${NEXUS_COMPOSE[@]}" up -d --no-deps --force-recreate api
  sudo zerotier-cli listnetworks
fi
