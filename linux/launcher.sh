#!/usr/bin/env bash
set -euo pipefail
operation="${1:-}"
case "$operation" in start|stop|restart|backup|admin) ;; *) echo 'Operação inválida.' >&2; exit 2 ;; esac
cd -- "$(dirname -- "$(dirname -- "$(readlink -f -- "$0")")")"
if [[ ! -t 0 ]]; then
  exec x-terminal-emulator -e bash "$PWD/linux/launcher.sh" "$operation"
fi
trap 'echo; read -r -p "Pressione Enter para fechar..." _ || true' EXIT
case "$operation" in
  start|stop|restart) sudo bash "linux/$operation.sh" ;;
  backup) sudo bash linux/backup.sh --destination /var/backups/nexus ;;
  admin)
    source linux/common.sh
    sudo "${NEXUS_COMPOSE[@]}" exec api python -m scripts.setup_admin --username admin ;;
esac
