#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
usage='Uso: restore.sh --archive DIRETORIO --safety-backup-dir DIRETORIO --confirm-project NOME [--dry-run]'
archive=''; safety_dir=''; confirmed=''; dry_run=false
while (($#)); do
  case "$1" in
    --archive) archive="${2:-}"; shift 2 ;;
    --safety-backup-dir) safety_dir="${2:-}"; shift 2 ;;
    --confirm-project) confirmed="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    --help) echo "$usage"; exit 0 ;;
    *) die "$usage" ;;
  esac
done
[[ -d "$archive" && -f "$archive/SHA256SUMS" && -f "$archive/database.dump" && -f "$archive/data.tar.gz" ]] || die 'Backup incompleto.'
(cd "$archive" && sha256sum -c SHA256SUMS) || die 'Falha de integridade.'
tar -tzf "$archive/data.tar.gz" | while IFS= read -r entry; do
  [[ "$entry" == data || "$entry" == data/ || "$entry" == data/* ]] || die 'Caminho inválido no arquivo de dados.'
  [[ "$entry" != *'/../'* && "$entry" != *'/./'* ]] || die 'Travessia de diretório no arquivo.'
done
tar -tvzf "$archive/data.tar.gz" | awk 'substr($1,1,1)=="l" || substr($1,1,1)=="h" {bad=1} END {exit bad}' || die 'Links em arquivo de dados não são aceitos.'
if "$dry_run"; then
  echo "DRY-RUN: backup íntegro; restauraria o projeto $NEXUS_PROJECT após backup de segurança."
  exit 0
fi
[[ "$confirmed" == "$NEXUS_PROJECT" ]] || die "Confirme o projeto exato com --confirm-project $NEXUS_PROJECT."
[[ "$safety_dir" == /* && -d "$safety_dir" && -w "$safety_dir" ]] || die 'Informe diretório absoluto existente para backup de segurança.'
preflight
"${NEXUS_COMPOSE[@]}" exec -T postgres pg_restore --list <"$archive/database.dump" >/dev/null
old_data="$NEXUS_ROOT/data.pre-restore.$(date -u +%Y%m%dT%H%M%SZ)"
[[ ! -e "$old_data" ]] || die "Diretório de preservação já existe: $old_data"
bash "$NEXUS_ROOT/linux/backup.sh" --destination "$safety_dir"
lock_operation
"${NEXUS_COMPOSE[@]}" stop api
"${NEXUS_COMPOSE[@]}" exec -T postgres dropdb --if-exists --force -U i9 i9
"${NEXUS_COMPOSE[@]}" exec -T postgres createdb -U i9 -O i9 i9
"${NEXUS_COMPOSE[@]}" exec -T postgres pg_restore --exit-on-error --no-owner --no-privileges -U i9 -d i9 <"$archive/database.dump"
mv -- "$NEXUS_ROOT/data" "$old_data"
tar -C "$NEXUS_ROOT" --no-same-owner --no-same-permissions -xzf "$archive/data.tar.gz"
"${NEXUS_COMPOSE[@]}" up -d --no-deps api
command -v curl >/dev/null 2>&1 || die 'curl ausente; verifique /health manualmente.'
healthy=false
for attempt in {1..30}; do
  if curl --silent --fail --max-time 3 "http://127.0.0.1:$(sed -n 's/^NEXUS_HOST_PORT=//p' "$NEXUS_ROOT/.env")/health" >/dev/null; then healthy=true; break; fi
  sleep 2
done
"$healthy" || die 'API não respondeu após restauração. Preserve os backups e consulte os logs.'
echo "Restauração concluída. Dados anteriores preservados em $old_data. Teste login e consultas antes de excluir qualquer arquivo."
