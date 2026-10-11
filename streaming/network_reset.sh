#!/usr/bin/env bash
# Run on the GX10. Removes the access-point pin so the GX10 can connect normally elsewhere.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

sudo nmcli con modify "$WIFI_CONN" 802-11-wireless.bssid ""
sudo nmcli con up "$WIFI_CONN"
echo "Access point pin removed from '$WIFI_CONN'."
