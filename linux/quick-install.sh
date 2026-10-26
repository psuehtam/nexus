#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

case "${1:-}" in
  --dry-run)
    bash "$NEXUS_ROOT/linux/diagnose.sh" --dry-run
    echo 'DRY-RUN: porta externa 8001, PostgreSQL e API em Docker, administrador interativo, systemd e backup diário.'
    echo 'DRY-RUN: ZeroTier opcional; a autorização do computador deve ser feita no painel da rede.'
    exit 0 ;;
  '') ;;
  *) die 'Uso: quick-install.sh [--dry-run]' ;;
esac
require_linux
# Este pacote é específico para o servidor informado; não tenta converter outra distribuição.
source /etc/os-release
[[ "${ID:-}" == linuxmint && "${VERSION_ID:-}" == 22.3 && "${UBUNTU_CODENAME:-}" == noble ]] ||
  die 'Pacote preparado para Linux Mint 22.3 (Ubuntu noble).'
[[ "$(uname -m)" == x86_64 ]] || die 'Pacote preparado para x86_64.'
for tool in ip ss curl systemctl sudo; do command -v "$tool" >/dev/null || die "$tool ausente."; done
sudo -v
if ! command -v docker >/dev/null || ! sudo docker compose version >/dev/null 2>&1 || ! sudo docker info >/dev/null 2>&1; then
  die 'Docker Engine e Compose devem estar funcionando. Não alterei a instalação Docker/Portainer existente.'
fi
mem_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
((mem_kib >= 3500000)) || die 'Menos de 3,5 GB de RAM total.'
free_kib="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)"
((free_kib >= 700000)) || die 'Menos de 700 MB disponíveis; libere memória antes de instalar.'
available_kib="$(df -Pk "$NEXUS_ROOT" | awk 'END {print $4}')"
((available_kib >= 20000000)) || die 'Menos de 20 GB livres na partição do projeto.'
if [[ ! -f "$NEXUS_ROOT/.env" ]]; then
  if ss -H -ltn '( sport = :8001 )' | grep -q .; then
    die 'Porta 8001 ocupada; nenhum contêiner foi alterado.'
  fi
  lan_ip=10.48.155.129
  if ! ip -4 -o addr show | grep -Fq "inet $lan_ip/"; then
    echo 'O IP esperado 10.48.155.129 não foi encontrado. Verifique a rede com a TI.'
    read -r -p 'IP local atual para o Nexus: ' lan_ip
    ip -4 -o addr show | grep -Fq "inet $lan_ip/" || die 'O IP informado não está configurado neste servidor.'
  fi
  sudo bash "$NEXUS_ROOT/linux/configure.sh" --bind-ip "$lan_ip" --port 8001
  new_install=true
else
  new_install=false
  echo '.env existente preservado; conferindo configuração.'
  sudo bash "$NEXUS_ROOT/linux/diagnose.sh"
  [[ "$(sudo sed -n 's/^NEXUS_HOST_PORT=//p' "$NEXUS_ROOT/.env")" == 8001 ]] ||
    die '.env existente não usa porta 8001. Revise antes de continuar.'
fi
sudo bash "$NEXUS_ROOT/linux/install.sh"
if "$new_install"; then
  echo 'Crie agora a senha do administrador do Nexus. A senha digitada não será gravada no instalador.'
  sudo "${NEXUS_COMPOSE[@]}" exec api python -m scripts.setup_admin --username admin
fi
if [[ ! -e /etc/systemd/system/nexus.service ]]; then
  sudo mkdir -p /var/backups/nexus
  sudo bash "$NEXUS_ROOT/linux/setup-systemd.sh" --backup-dir /var/backups/nexus --confirm
else
  echo 'nexus.service já existe; não será sobrescrito.'
fi
echo 'Instalação local concluída.'
echo "Acesso LAN: http://$(sudo sed -n 's/^NEXUS_BIND_IP=//p' "$NEXUS_ROOT/.env"):8001/veiculos"
read -r -p 'Network ID do ZeroTier (Enter para configurar depois): ' network_id
if [[ -n "$network_id" ]]; then
  if bash "$NEXUS_ROOT/linux/zerotier.sh" --network-id "$network_id"; then
    if [[ -f "$NEXUS_ROOT/linux/compose.zerotier.yaml" ]]; then
      source "$NEXUS_ROOT/linux/common.sh"
      sudo "${NEXUS_COMPOSE[@]}" up -d --no-deps --force-recreate api
      echo 'Acesso ZeroTier: veja o IP em sudo zerotier-cli listnetworks, porta 8001.'
    fi
  else
    echo 'ZeroTier precisa de atenção. O acesso local ao Nexus continua disponível.'
  fi
fi
echo 'Teste em um computador da rede local. Firewall, IP fixo e HTTPS dependem da TI.'
