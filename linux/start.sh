#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
case "${1:-}" in
  '') nexus_run up -d postgres api ;;
  --dry-run) nexus_run --dry-run up -d postgres api ;;
  --help) echo 'Uso: start.sh [--dry-run]' ;;
  *) echo 'Argumento inválido' >&2; exit 2 ;;
esac
