#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
if [[ ! -t 0 ]]; then
  exec x-terminal-emulator -e bash "$PWD/04-TESTAR-INICIALIZACAO.sh"
fi
trap 'echo; read -r -p "Pressione Enter para fechar..." _ || true' EXIT
[[ -f .env ]] || { echo 'Instale o Nexus primeiro (etapa 02).'; exit 1; }
source linux/common.sh
sudo -v
failed=0
check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "OK: $label"
  else
    echo "FALHOU: $label"
    failed=1
  fi
}
check 'Docker ativo' sudo systemctl is-active --quiet docker.service
check 'Nexus habilitado para iniciar com o sistema' sudo systemctl is-enabled --quiet nexus.service
check 'Serviço Nexus ativo' sudo systemctl is-active --quiet nexus.service
check 'Backup diário habilitado' sudo systemctl is-enabled --quiet nexus-backup.timer
check 'Timer de backup ativo' sudo systemctl is-active --quiet nexus-backup.timer
check 'PostgreSQL rodando' bash -c 'sudo docker compose --project-name nexus -f compose.yaml -f linux/compose.linux.yaml ps --status running --services | grep -Fxq postgres'
check 'API rodando' bash -c 'sudo docker compose --project-name nexus -f compose.yaml -f linux/compose.linux.yaml ps --status running --services | grep -Fxq api'
check 'API configurada para reiniciar' bash -c '[[ "$(sudo docker inspect --format "{{.HostConfig.RestartPolicy.Name}}" nexus-api-1)" == unless-stopped ]]'
check 'PostgreSQL configurado para reiniciar' bash -c '[[ "$(sudo docker inspect --format "{{.HostConfig.RestartPolicy.Name}}" nexus-postgres-1)" == unless-stopped ]]'
check 'Saúde local em 8001' curl --silent --fail --max-time 5 http://127.0.0.1:8001/health
lan_ip="$(sudo sed -n 's/^NEXUS_BIND_IP=//p' .env)"
check "Saúde LAN em $lan_ip:8001" curl --silent --fail --max-time 5 "http://$lan_ip:8001/health"
if ((failed)); then
  echo 'A inicialização ainda não está pronta. Copie os itens FALHOU para diagnóstico.'
  exit 1
fi
boot_id="$(cat /proc/sys/kernel/random/boot_id)"
record=linux/.ultimo-boot-testado
if [[ -f "$record" && "$(cat "$record")" != "$boot_id" ]]; then
  echo 'PASSOU: o computador reiniciou desde o teste anterior e o Nexus voltou a responder.'
else
  echo 'Configuração de início automático aprovada. Para comprovar após reinício, reinicie o PC em horário autorizado e abra este arquivo de novo.'
fi
printf '%s\n' "$boot_id" >"$record"
