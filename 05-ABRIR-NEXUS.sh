#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
[[ -f .env ]] || { echo 'Instale o Nexus primeiro (etapa 02).'; exit 1; }
ip="$(sudo sed -n 's/^NEXUS_BIND_IP=//p' .env)"
port="$(sudo sed -n 's/^NEXUS_HOST_PORT=//p' .env)"
[[ -n "$ip" && -n "$port" ]] || { echo 'IP ou porta não configurados em .env.'; exit 1; }
xdg-open "http://$ip:$port/veiculos"
