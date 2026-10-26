#!/usr/bin/env bash
exec bash "$(dirname -- "$(readlink -f -- "$0")")/linux/launcher.sh" restart
