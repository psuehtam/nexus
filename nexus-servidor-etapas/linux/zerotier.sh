#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

usage='Uso: zerotier.sh --network-id ID_DE_16_HEX [--dry-run]'
network_id=''; dry_run=false
while (($#)); do
  case "$1" in
    --network-id) network_id="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    --help) echo "$usage"; exit 0 ;;
    *) die "$usage" ;;
  esac
done
[[ "$network_id" =~ ^[0-9a-fA-F]{16}$ ]] || die 'Network ID do ZeroTier deve ter 16 caracteres hexadecimais.'
if "$dry_run"; then
  echo "DRY-RUN: verificaria ZeroTier, entraria na rede $network_id e, após autorização, publicaria a porta 8001 no IP virtual."
  exit 0
fi
require_linux
[[ "$(uname -m)" == x86_64 ]] || die 'Este pacote foi preparado para x86_64.'
command -v curl >/dev/null || die 'curl necessário para instalar o ZeroTier.'
if ! command -v zerotier-cli >/dev/null 2>&1; then
  echo 'Instalando ZeroTier pela fonte oficial...'
  installer="$(mktemp)"
  trap 'rm -f -- "$installer"' EXIT
  curl -fsSL https://install.zerotier.com -o "$installer"
  if ! sudo bash "$installer"; then
    # O instalador oficial ainda pode gerar uma fonte APT incompleta no Mint 22.3.
    key=/usr/share/keyrings/zerotier-debian-package-key.gpg
    [[ -s "$key" ]] || die 'Instalação oficial falhou sem criar chave APT; nenhuma fonte será improvisada.'
    printf '%s\n' "deb [signed-by=$key] http://download.zerotier.com/debian/noble noble main" |
      sudo tee /etc/apt/sources.list.d/zerotier.list >/dev/null
    sudo apt-get update
    sudo apt-get install -y zerotier-one
  fi
  trap - EXIT
  rm -f -- "$installer"
fi
sudo systemctl enable --now zerotier-one
sudo zerotier-cli join "$network_id"
assigned_ip="$(sudo zerotier-cli listnetworks | awk -v id="$network_id" '$3 == id && $0 ~ / OK PRIVATE / {print $NF}' | cut -d/ -f1)"
if [[ ! "$assigned_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo 'ZeroTier instalado. Autorize este computador em Members no painel da rede e abra 03-ATIVAR-ZEROTIER.sh depois.'
  exit 0
fi
[[ "$assigned_ip" != 0.0.0.0 && "$assigned_ip" != 127.* ]] || die 'IP virtual inválido.'
cat >"$NEXUS_ROOT/linux/compose.zerotier.yaml.tmp" <<EOF
services:
  api:
    ports:
      - "$assigned_ip:\${NEXUS_HOST_PORT:-8001}:8000"
EOF
mv -f -- "$NEXUS_ROOT/linux/compose.zerotier.yaml.tmp" "$NEXUS_ROOT/linux/compose.zerotier.yaml"
echo "ZeroTier autorizado no IP $assigned_ip. A publicação entra em vigor quando a API iniciar/reiniciar."
