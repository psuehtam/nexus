#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
case "${1:-}" in
  --help) echo 'Uso: install.sh [--dry-run]'; exit 0 ;;
  --dry-run) echo 'DRY-RUN: validar configuração, construir imagem, iniciar PostgreSQL, aplicar migrações, iniciar API e checar saúde.'; exit 0 ;;
  '') ;;
  *) die 'Argumento inválido.' ;;
esac
preflight
lock_operation
"${NEXUS_COMPOSE[@]}" build api migrate
"${NEXUS_COMPOSE[@]}" up -d postgres
"${NEXUS_COMPOSE[@]}" run --rm migrate
"${NEXUS_COMPOSE[@]}" up -d --no-deps api
command -v curl >/dev/null 2>&1 || die 'curl ausente; saúde da API não verificada.'
healthy=false
for attempt in {1..30}; do
  if curl --silent --fail --max-time 3 "http://127.0.0.1:$(sed -n 's/^NEXUS_HOST_PORT=//p' "$NEXUS_ROOT/.env")/health" >/dev/null; then healthy=true; break; fi
  sleep 2
done
[[ "$healthy" == true ]] || die 'API não respondeu em /health; inspecione os logs.'
echo 'API saudável. Crie o administrador com o comando completo indicado em linux/README.md.'
