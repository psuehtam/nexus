#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
usage='Uso: setup-systemd.sh --backup-dir DIRETORIO [--dry-run|--confirm]'
backup_dir=''; mode=''
while (($#)); do
  case "$1" in
    --backup-dir) backup_dir="${2:-}"; shift 2 ;;
    --dry-run|--confirm) mode="$1"; shift ;;
    --help) echo "$usage"; exit 0 ;;
    *) die "$usage" ;;
  esac
done
[[ "$backup_dir" == /* ]] || die 'Diretório absoluto de backup obrigatório.'
[[ "$mode" == --dry-run || "$mode" == --confirm ]] || die "$usage"
[[ "$NEXUS_ROOT" != *' '* && "$NEXUS_ROOT" != *'|'* && "$NEXUS_ROOT" != *'&'* && "$backup_dir" != *' '* && "$backup_dir" != *'|'* && "$backup_dir" != *'&'* ]] || die 'Use caminhos sem espaços ou caracteres especiais.'
if [[ "$mode" == --dry-run ]]; then
  echo "DRY-RUN: instalaria nexus.service e timer diário de backup usando $NEXUS_ROOT e $backup_dir."
  exit 0
fi
preflight
[[ "$(id -u)" == 0 ]] || die 'Execute como root para instalar units systemd.'
[[ "$(ps -p 1 -o comm=)" == systemd ]] || die 'Este Linux não usa systemd como init.'
[[ -d "$backup_dir" && -w "$backup_dir" ]] || die 'Diretório de backup deve existir e ser gravável.'
for unit in nexus.service nexus-backup.service nexus-backup.timer; do
  [[ ! -e "/etc/systemd/system/$unit" ]] || die "Unit existente: $unit. Não será sobrescrita."
done
sed "s|/opt/nexus|$NEXUS_ROOT|g" "$NEXUS_ROOT/linux/systemd/nexus.service.example" >/etc/systemd/system/nexus.service
sed -e "s|/opt/nexus|$NEXUS_ROOT|g" -e "s|/var/backups/nexus|$backup_dir|g" "$NEXUS_ROOT/linux/systemd/nexus-backup.service.example" >/etc/systemd/system/nexus-backup.service
cp -- "$NEXUS_ROOT/linux/systemd/nexus-backup.timer.example" /etc/systemd/system/nexus-backup.timer
systemctl daemon-reload
systemctl enable --now nexus.service nexus-backup.timer
echo 'Serviço e timer de backup instalados. Confira systemctl status nexus.service e systemctl list-timers nexus-backup.timer.'
