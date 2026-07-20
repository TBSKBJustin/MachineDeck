#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
user_unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"

install -d -m 0755 "${user_unit_dir}"
install -m 0644 "${repository_root}/systemd/machinedeck-phase0.service" "${user_unit_dir}/machinedeck-phase0.service"
systemctl --user daemon-reload

echo "Installed machinedeck-phase0.service for the current user."
echo "Run: systemctl --user start machinedeck-phase0.service"

