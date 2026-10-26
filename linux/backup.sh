#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
case "${1:-}" in
  --help) echo 'Uso: backup.sh --destination DIRETORIO [--dry-run]'; exit 0 ;;
  --destination) destination="${2:-}"; mode="${3:-}" ;;
  *) die 'Uso: backup.sh --destination DIRETORIO [--dry-run]' ;;
esac
[[ -n "$destination" && "$destination" == /* ]] || die 'Destino absoluto obrigatório.'
[[ -z "$mode" || "$mode" == --dry-run ]] || die 'Argumento inválido.'
if [[ "$mode" == --dry-run ]]; then echo "DRY-RUN: criaria dump PostgreSQL e cópia de data/ em $destination"; exit 0; fi
preflight
lock_operation
[[ -d "$destination" && -w "$destination" ]] || die 'Destino existente e gravável obrigatório.'
case "$(cd -- "$destination" && pwd -P)/" in "$NEXUS_ROOT/"*) die 'Destino deve ficar fora do projeto.' ;; esac
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$destination/nexus-$stamp"
[[ ! -e "$target" ]] || die "Backup já existe: $target"
tmp="$(mktemp -d "$destination/.nexus-backup-$stamp.XXXXXX")"
api_was_running=false
if [[ "$("${NEXUS_COMPOSE[@]}" ps --status running --services api)" == api ]]; then
  api_was_running=true
  "${NEXUS_COMPOSE[@]}" stop api
fi
cleanup() {
  if "$api_was_running"; then "${NEXUS_COMPOSE[@]}" up -d --no-deps api >&2 || true; fi
  [[ ! -d "$tmp" ]] || rm -rf -- "$tmp"
}
trap cleanup EXIT
"${NEXUS_COMPOSE[@]}" exec -T postgres pg_dump -U i9 -d i9 -Fc >"$tmp/database.dump"
[[ -s "$tmp/database.dump" ]] || die 'Dump vazio.'
tar -C "$NEXUS_ROOT" -czf "$tmp/data.tar.gz" data
(cd "$tmp" && sha256sum database.dump data.tar.gz >SHA256SUMS)
mv -- "$tmp" "$target"
cleanup
trap - EXIT
echo "Backup criado em $target. Verifique periodicamente por restauração em ambiente isolado."
