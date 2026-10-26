#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
case "${1:-}" in
  --help) echo 'Uso: remove-systemd.sh [--dry-run|--confirm]. Remove somente as units NEXUS instaladas para esta pasta.'; exit 0 ;;
  --dry-run) echo 'DRY-RUN: desabilitaria e removeria nexus.service, nexus-backup.service e nexus-backup.timer se pertencerem a esta pasta.'; exit 0 ;;
  --confirm) ;;
  *) die 'Use --confirm ou --dry-run.' ;;
esac
require_linux
[[ "$(id -u)" == 0 ]] || die 'Execute como root.'
service=/etc/systemd/system/nexus.service
[[ -f "$service" ]] || die 'nexus.service não está instalada.'
grep -Fxq "WorkingDirectory=$NEXUS_ROOT" "$service" || die 'Unit não pertence a esta pasta.'
systemctl disable --now nexus-backup.timer nexus.service
rm -f -- /etc/systemd/system/nexus.service /etc/systemd/system/nexus-backup.service /etc/systemd/system/nexus-backup.timer
systemctl daemon-reload
echo 'Units NEXUS removidas. Dados e contêineres preservados.'
