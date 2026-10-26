#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
if [[ ! -t 0 ]]; then
  exec x-terminal-emulator -e bash "$PWD/01-VERIFICAR-SERVIDOR.sh"
fi
trap 'echo; read -r -p "Pressione Enter para fechar..." _ || true' EXIT
bash linux/diagnose.sh
echo
echo 'Portas 8000 (Portainer) e 8001 (Nexus):'
ss -ltn '( sport = :8000 or sport = :8001 )'
