#!/usr/bin/env bash
set -euo pipefail

NEXUS_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
NEXUS_PROJECT="${NEXUS_PROJECT:-nexus}"
NEXUS_COMPOSE=(docker compose --project-name "$NEXUS_PROJECT" --project-directory "$NEXUS_ROOT" -f "$NEXUS_ROOT/compose.yaml" -f "$NEXUS_ROOT/linux/compose.linux.yaml")
if [[ -f "$NEXUS_ROOT/linux/compose.zerotier.yaml" ]]; then
  NEXUS_COMPOSE+=(-f "$NEXUS_ROOT/linux/compose.zerotier.yaml")
fi

die() { echo "ERRO: $*" >&2; exit 2; }
require_linux() { [[ "$(uname -s)" == Linux ]] || die 'Operação disponível apenas no Linux.'; }
require_docker() {
  command -v docker >/dev/null 2>&1 || die 'Docker ausente.'
  docker compose version >/dev/null 2>&1 || die 'Plugin Compose ausente.'
  docker info >/dev/null 2>&1 || die 'Daemon Docker inacessível.'
}
require_config() {
  [[ -f "$NEXUS_ROOT/.env" ]] || die 'Execute configure.sh primeiro.'
  grep -Eq '^POSTGRES_PASSWORD=.{16,}$' "$NEXUS_ROOT/.env" || die 'POSTGRES_PASSWORD inválida.'
  grep -Eq '^ADMIN_API_KEY=.{24,}$' "$NEXUS_ROOT/.env" || die 'ADMIN_API_KEY inválida.'
  ! grep -q 'CHANGE_ME' "$NEXUS_ROOT/.env" || die 'Valores de exemplo recusados.'
  grep -Eq '^NEXUS_BIND_IP=([0-9]{1,3}\.){3}[0-9]{1,3}$' "$NEXUS_ROOT/.env" || die 'Defina NEXUS_BIND_IP como IPv4 específico.'
  ! grep -q '^NEXUS_BIND_IP=0.0.0.0$' "$NEXUS_ROOT/.env" || die 'Bind em todas as interfaces recusado.'
  grep -Eq '^NEXUS_HOST_PORT=[0-9]{1,5}$' "$NEXUS_ROOT/.env" || die 'Defina NEXUS_HOST_PORT.'
}
preflight() {
  require_linux
  [[ -f "$NEXUS_ROOT/compose.yaml" && -f "$NEXUS_ROOT/linux/compose.linux.yaml" ]] || die 'Arquivos Compose ausentes.'
  require_config
  require_docker
  "${NEXUS_COMPOSE[@]}" config --quiet
}
lock_operation() {
  command -v flock >/dev/null 2>&1 || die 'flock ausente.'
  exec 9>"$NEXUS_ROOT/linux/.operation.lock"
  flock -n 9 || die 'Outra operação NEXUS está em andamento.'
}

nexus_run() {
  if [[ "${1:-}" == '--dry-run' ]]; then
    shift
    printf 'DRY-RUN: '
    printf '%q ' "${NEXUS_COMPOSE[@]}" "$@"
    printf '\n'
  else
    preflight
    lock_operation
    "${NEXUS_COMPOSE[@]}" "$@"
  fi
}
