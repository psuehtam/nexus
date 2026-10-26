#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
case "${1:-}" in
  '') nexus_run stop api postgres ;;
  --dry-run) nexus_run --dry-run stop api postgres ;;
  --help) echo 'Uso: stop.sh [--dry-run]' ;;
  *) echo 'Argumento inválido' >&2; exit 2 ;;
esac
