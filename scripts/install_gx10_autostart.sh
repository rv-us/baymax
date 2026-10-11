#!/usr/bin/env bash
# Make the GX10 start its Baymax services at boot (needs sudo; run on the GX10).
#
#     ./scripts/install_gx10_autostart.sh            # install and enable
#     ./scripts/install_gx10_autostart.sh remove     # undo
#
# Status: systemctl status baymax-gx10    Logs: journalctl -u baymax-gx10, then ~/*_service.log
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" = remove ]; then
  sudo systemctl disable --now baymax-gx10 || true
  sudo rm -f /etc/systemd/system/baymax-gx10.service
  sudo systemctl daemon-reload
  echo "autostart removed"
  exit 0
fi
sudo install -m 644 baymax-gx10.service /etc/systemd/system/baymax-gx10.service
sudo systemctl daemon-reload
sudo systemctl enable baymax-gx10
echo "enabled: the services start at every boot (now: sudo systemctl start baymax-gx10, or ./scripts/start_gx10.sh)"
