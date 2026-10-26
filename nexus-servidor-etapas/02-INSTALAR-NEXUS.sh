#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
if [[ ! -t 0 ]]; then
  exec x-terminal-emulator -e bash "$PWD/02-INSTALAR-NEXUS.sh"
fi
trap 'echo; read -r -p "Pressione Enter para fechar..." _ || true' EXIT
bash linux/quick-install.sh
