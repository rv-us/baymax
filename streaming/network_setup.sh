#!/usr/bin/env bash
# Run on the GX10. Applies the Wi-Fi fixes that made streaming smooth:
#   1. Turns off Wi-Fi power saving on the Pi and the GX10
#   2. Pins the GX10 to the same access point (BSSID) the Pi is using
# Undo the pin with network_reset.sh when you leave the venue.
set -euo pipefail
cd "$(dirname "$0")"
source ./config.env

echo "== Pi: disabling Wi-Fi power saving"
ssh "$PI_HOST" "sudo /usr/sbin/iw dev $PI_IFACE set power_save off && sudo /usr/sbin/iw dev $PI_IFACE get power_save"

echo "== Pi: reading access point"
PI_BSSID=$(ssh "$PI_HOST" "sudo /usr/sbin/iw dev $PI_IFACE link" | awk '/Connected to/ {print $3}')
if [[ -z "$PI_BSSID" ]]; then
  echo "Could not read the Pi's access point. Is it connected to Wi-Fi?" >&2
  exit 1
fi
echo "Pi is on access point $PI_BSSID"

echo "== GX10: disabling Wi-Fi power saving (persistent)"
sudo nmcli con modify "$WIFI_CONN" 802-11-wireless.powersave 2

CURRENT_BSSID=$(sudo iw dev "$GX10_IFACE" link | awk '/Connected to/ {print $3}')
if [[ "$CURRENT_BSSID" == "$PI_BSSID" ]]; then
  echo "GX10 is already on the same access point."
  sudo iw dev "$GX10_IFACE" set power_save off
else
  echo "== GX10: pinning to $PI_BSSID (was ${CURRENT_BSSID:-not connected})"
  echo "   If you're SSH'd in over Wi-Fi, your session may drop for a few seconds."
  sudo nmcli con modify "$WIFI_CONN" 802-11-wireless.bssid "$PI_BSSID"
  if ! sudo nmcli con up "$WIFI_CONN"; then
    echo "Could not connect to $PI_BSSID (probably out of range). Removing the pin." >&2
    sudo nmcli con modify "$WIFI_CONN" 802-11-wireless.bssid ""
    sudo nmcli con up "$WIFI_CONN"
    exit 1
  fi
fi

echo "== Checking the link"
sudo iw dev "$GX10_IFACE" link | grep -E "Connected to|freq|signal"
GATEWAY=$(ip route | awk '/^default/ && /'"$GX10_IFACE"'/ {print $3; exit}')
if [[ -n "$GATEWAY" ]]; then
  echo "Pinging gateway $GATEWAY:"
  ping -c 30 -i 0.2 "$GATEWAY" | tail -2
fi
echo "Done. Run network_reset.sh before leaving this venue."
