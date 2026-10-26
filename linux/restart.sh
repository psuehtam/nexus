#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
case "${1:-}" in
  '') nexus_run restart api ;;
  --dry-run) nexus_run --dry-run restart api ;;
  --help) echo 'Uso: restart.sh [--dry-run] (somente API)' ;;
  *) echo 'Argumento inválido' >&2; exit 2 ;;
esac
