#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  ''|--dry-run) ;;
  --help) echo 'Uso: diagnose.sh [--dry-run]  (somente leitura)'; exit 0 ;;
  *) echo 'Argumento inválido' >&2; exit 2 ;;
esac

echo 'NEXUS — diagnóstico local (somente leitura; sem segredos)'
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  printf 'Sistema: %s\n' "${PRETTY_NAME:-desconhecido}"
else
  echo 'Sistema: /etc/os-release indisponível'
fi
printf 'Arquitetura: %s\n' "$(uname -m)"
printf 'Init: %s\n' "$(ps -p 1 -o comm= 2>/dev/null || echo indisponível)"
for binary in docker systemctl flock curl; do
  if command -v "$binary" >/dev/null 2>&1; then
    printf '%s: presente\n' "$binary"
  else
    printf '%s: ausente\n' "$binary"
  fi
done
if command -v docker >/dev/null 2>&1; then
  docker --version 2>/dev/null || true
  docker compose version 2>/dev/null || echo 'Plugin Compose indisponível'
  docker info --format 'Daemon acessível: {{.ServerVersion}}' 2>/dev/null || echo 'Daemon Docker inacessível para este usuário'
fi
df -h . 2>/dev/null || true
echo 'IP, firewall, DNS, proxy e espaço de backup: confirmar com a equipe de infraestrutura.'
