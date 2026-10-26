#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
case "${1:-}" in
  --help) echo 'Uso: uninstall.sh --confirm [--dry-run]. Remove somente contêineres/rede do projeto; preserva volumes, código, .env e backups.'; exit 0 ;;
  --dry-run) nexus_run --dry-run down; exit 0 ;;
  --confirm) ;;
  *) die 'Use --confirm ou --dry-run.' ;;
esac
require_linux
require_config
require_docker
lock_operation
if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet nexus.service 2>/dev/null; then
  die 'Desabilite nexus.service antes de remover os contêineres, para impedir reinicialização automática.'
fi
"${NEXUS_COMPOSE[@]}" down
echo 'Contêineres e rede do projeto removidos. Volumes do banco, arquivos, configuração e backups preservados.'
