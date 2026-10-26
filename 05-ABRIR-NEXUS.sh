#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
xdg-open 'http://10.48.155.129:8001/veiculos'
