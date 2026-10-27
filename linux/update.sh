#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
usage='Uso: update.sh --backup-dir DIRETORIO --confirm [--dry-run]. Atualiza o código já colocado nesta pasta; não faz git pull.'
backup_dir=''
confirm=false
dry_run=false
while (($#)); do
  case "$1" in
    --backup-dir) backup_dir="${2:-}"; shift 2 ;;
    --confirm) confirm=true; shift ;;
    --dry-run) dry_run=true; shift ;;
    --help) echo "$usage"; exit 0 ;;
    *) die "$usage" ;;
  esac
done
[[ "$backup_dir" == /* ]] || die 'Informe diretório absoluto para backup.'
if "$dry_run"; then
  echo "DRY-RUN: backup em $backup_dir; build; migração; recriação da API; verificação de /health."
  exit 0
fi
"$confirm" || die 'Use --confirm para executar a atualização.'
preflight
[[ -d "$backup_dir" && -w "$backup_dir" ]] || die 'Diretório de backup inexistente ou sem escrita.'
bash "$NEXUS_ROOT/linux/backup.sh" --destination "$backup_dir"
lock_operation
"${NEXUS_COMPOSE[@]}" build api migrate
"${NEXUS_COMPOSE[@]}" stop api
"${NEXUS_COMPOSE[@]}" run --rm migrate
"${NEXUS_COMPOSE[@]}" up -d --no-deps --force-recreate api
command -v curl >/dev/null 2>&1 || die 'curl ausente.'
healthy=false
for attempt in {1..30}; do
  if curl --silent --fail --max-time 3 "http://127.0.0.1:$(sed -n 's/^NEXUS_HOST_PORT=//p' "$NEXUS_ROOT/.env")/health" >/dev/null; then healthy=true; break; fi
  sleep 2
done
"$healthy" || die 'API não ficou saudável. Preserve o backup; migrações podem impedir rollback somente da imagem.'
echo 'Atualização concluída. Confira login e telas principais em outro dispositivo da LAN.'
