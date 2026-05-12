#!/bin/bash
# setup_networking.sh — configure eth0 dual-IP and wlan0 isolated AP for racecar.
#
# This script:
#   1. Installs a NetworkManager dispatcher that blocks FORWARD on wlan0 so AP
#      clients can reach the Pi's services (dashboard, jupyter, SSH) but cannot
#      use the Pi as an internet gateway.
#   2. Removes any prior Wi-Fi client connection on wlan0 (e.g. a leftover
#      eduroam/home-WiFi connection from a previous setup).
#   3. Creates the racecar AP NetworkManager connection on wlan0 (WPA2 / 2.4 GHz
#      / channel 6 / 10.42.0.1/24).
#   4. Writes /etc/netplan/99-racecar-eth0.yaml so eth0 carries both a static
#      address (default 192.168.52.200/24) and a DHCP-assigned address.
#   5. Runs `netplan apply` to bring the new config up.
#
# WARNING: this script reconfigures wlan0. If you're SSH'd in over WiFi, the
# connection will drop when the AP comes up. Run from a wired (eth0) session
# or directly on the console.
#
# Parameters (override via environment variables before running):
#   RACECAR_AP_SSID       (default: racecar-neo-1)
#   RACECAR_AP_PSK        (default: racecar@mit)
#   RACECAR_AP_CHANNEL    (default: 6)
#   RACECAR_AP_ADDR       (default: 10.42.0.1/24)
#   RACECAR_ETH_STATIC    (default: 192.168.52.200/24)
#
# All steps are idempotent — re-running is safe.

set -e

# Persisted overrides — `racecar setup networking --ssid=...` writes here.
# Precedence: env vars in the current shell > persisted file > defaults.
USER_HOME="$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)"
PERSIST_FILE="${USER_HOME}/.config/racecar/networking.env"
if [ -f "$PERSIST_FILE" ]; then
    # Only load keys not already set in the environment so the caller can
    # always override with `KEY=value racecar setup networking`.
    while IFS='=' read -r key val; do
        [ -z "$key" ] && continue
        [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
        if [ -z "${!key:-}" ]; then
            # Strip leading/trailing quotes from val (sh writes "value").
            val="${val%\"}"
            val="${val#\"}"
            export "$key=$val"
        fi
    done < "$PERSIST_FILE"
fi

echo "=== RACECAR Neo Networking Setup ==="

AP_SSID="${RACECAR_AP_SSID:-racecar-neo-1}"
AP_PSK="${RACECAR_AP_PSK:-racecar@mit}"
AP_CON_NAME="racecar-neo-ap"
AP_BAND="bg"
AP_CHANNEL="${RACECAR_AP_CHANNEL:-6}"
AP_ADDR="${RACECAR_AP_ADDR:-10.42.0.1/24}"

ETH_STATIC_ADDR="${RACECAR_ETH_STATIC:-192.168.52.200/24}"

DISPATCHER_PATH="/etc/NetworkManager/dispatcher.d/99-racecar-ap-isolate"
NETPLAN_ETH_PATH="/etc/netplan/99-racecar-eth0.yaml"

CHANGES_MADE=false

# --- 1. AP-isolation dispatcher ----------------------------------------------
echo "[1/4] Installing AP isolation dispatcher at $DISPATCHER_PATH..."
TMP_DISPATCHER=$(mktemp)
cat >"$TMP_DISPATCHER" <<SCRIPT
#!/bin/sh
# RACECAR Neo hotspot isolation — NM's ipv4.method=shared enables IP forwarding
# and sets up NAT, which would let wlan0 AP clients route out through eth0.
# Block FORWARD in/out of wlan0 so clients can reach the Pi's own services
# (dashboard, jupyter, SSH) but cannot use the Pi as an internet gateway.

iface="\$1"
action="\$2"

[ "\$iface" = "wlan0" ] || exit 0
[ "\$CONNECTION_ID" = "$AP_CON_NAME" ] || exit 0

case "\$action" in
    up)
        iptables -D FORWARD -i wlan0 -j REJECT 2>/dev/null
        iptables -D FORWARD -o wlan0 -j REJECT 2>/dev/null
        iptables -I FORWARD -i wlan0 -j REJECT
        iptables -I FORWARD -o wlan0 -j REJECT
        ;;
    down|pre-down)
        iptables -D FORWARD -i wlan0 -j REJECT 2>/dev/null
        iptables -D FORWARD -o wlan0 -j REJECT 2>/dev/null
        ;;
esac
exit 0
SCRIPT
if sudo cmp -s "$TMP_DISPATCHER" "$DISPATCHER_PATH" 2>/dev/null; then
    echo "  $DISPATCHER_PATH already up to date."
else
    sudo install -m 755 -o root -g root "$TMP_DISPATCHER" "$DISPATCHER_PATH"
    echo "  Dispatcher installed."
    CHANGES_MADE=true
fi
rm -f "$TMP_DISPATCHER"

# The dispatcher is socket-activated by NetworkManager-dispatcher.service.
# That service is shipped enabled by default on Ubuntu Server but is often
# disabled on Desktop / Raspberry Pi OS images. Without it the dispatcher
# script is never invoked and the iptables isolation rules silently never
# apply (exactly the failure mode v0.0.6 hit on first install).
#
# The service is Type=simple with no RemainAfterExit, so it shows "inactive"
# whenever no script is currently running — `is-active` is the wrong probe.
# `is-enabled` is the property we actually care about: will systemd start it
# the next time NM emits a connection event?
if ! systemctl is-enabled --quiet NetworkManager-dispatcher.service; then
    echo "  Enabling NetworkManager-dispatcher.service..."
    sudo systemctl enable --now NetworkManager-dispatcher.service
    CHANGES_MADE=true
fi

# --- 2. Delete prior Wi-Fi client connections on wlan0 -----------------------
echo "[2/4] Removing any prior Wi-Fi client connection on wlan0..."
mapfile -t prior_wifi < <(
    nmcli -t -f NAME,TYPE,DEVICE con show |
    awk -F: -v ap="$AP_CON_NAME" '
        $2 == "802-11-wireless" && $1 != ap { print $1 }
    '
)
if [ "${#prior_wifi[@]}" -eq 0 ]; then
    echo "  No prior Wi-Fi client connections found."
else
    for con in "${prior_wifi[@]}"; do
        echo "  Deleting connection '$con'..."
        sudo nmcli connection delete "$con"
        CHANGES_MADE=true
    done
fi

# --- 3. Create or update the AP connection -----------------------------------
echo "[3/4] Configuring AP connection '$AP_CON_NAME' (SSID: $AP_SSID)..."
if nmcli -t -f NAME con show | grep -qx "$AP_CON_NAME"; then
    # Diff each user-tunable setting against what nmcli reports; only call
    # `nmcli connection modify` when at least one field differs. (modify
    # always returns 0 even on no-op, so we can't rely on its exit code
    # to detect change.) PSK is hidden by default — use `--show-secrets`.
    nmcli_get() { sudo nmcli --show-secrets -g "$1" con show "$AP_CON_NAME" 2>/dev/null; }
    diff_ap=false
    for spec in \
        "802-11-wireless.ssid=$AP_SSID" \
        "802-11-wireless.mode=ap" \
        "802-11-wireless.band=$AP_BAND" \
        "802-11-wireless.channel=$AP_CHANNEL" \
        "802-11-wireless-security.key-mgmt=wpa-psk" \
        "802-11-wireless-security.psk=$AP_PSK" \
        "ipv4.method=shared" \
        "ipv4.addresses=$AP_ADDR" \
        "connection.autoconnect=yes"; do
        key="${spec%%=*}"; want="${spec#*=}"
        have=$(nmcli_get "$key" || true)
        if [ "$have" != "$want" ]; then
            diff_ap=true
            break
        fi
    done
    if [ "$diff_ap" = "true" ]; then
        echo "  Settings differ — applying."
        sudo nmcli connection modify "$AP_CON_NAME" \
            802-11-wireless.ssid "$AP_SSID" \
            802-11-wireless.mode ap \
            802-11-wireless.band "$AP_BAND" \
            802-11-wireless.channel "$AP_CHANNEL" \
            802-11-wireless-security.key-mgmt wpa-psk \
            802-11-wireless-security.psk "$AP_PSK" \
            ipv4.method shared \
            ipv4.addresses "$AP_ADDR" \
            connection.autoconnect yes
        CHANGES_MADE=true
    else
        echo "  Connection already matches desired settings."
    fi
else
    echo "  Creating new AP connection..."
    sudo nmcli connection add \
        type wifi \
        ifname wlan0 \
        con-name "$AP_CON_NAME" \
        autoconnect yes \
        ssid "$AP_SSID" \
        802-11-wireless.mode ap \
        802-11-wireless.band "$AP_BAND" \
        802-11-wireless.channel "$AP_CHANNEL" \
        802-11-wireless-security.key-mgmt wpa-psk \
        802-11-wireless-security.psk "$AP_PSK" \
        ipv4.method shared \
        ipv4.addresses "$AP_ADDR"
    CHANGES_MADE=true
fi

# Bring the AP up only if it isn't already, OR if settings just changed
# (changes require a cycle to take effect). Avoids momentarily dropping AP
# clients during no-op re-runs.
ap_state=$(nmcli -t -f GENERAL.STATE con show "$AP_CON_NAME" 2>/dev/null | head -1)
if [ "$CHANGES_MADE" = "true" ] || [ "$ap_state" != "activated" ]; then
    sudo nmcli connection up "$AP_CON_NAME" >/dev/null 2>&1 || true
fi

# --- 4. eth0 dual-IP via netplan ---------------------------------------------
echo "[4/4] Configuring eth0 dual-IP (static $ETH_STATIC_ADDR + DHCP)..."
TMP_NETPLAN=$(mktemp)
cat >"$TMP_NETPLAN" <<YAML
network:
  version: 2
  ethernets:
    eth0:
      renderer: NetworkManager
      addresses:
      - "$ETH_STATIC_ADDR"
      dhcp4: true
      dhcp6: true
      optional: true
      networkmanager:
        passthrough:
          ipv4.method: "auto"
          ipv4.address1: "$ETH_STATIC_ADDR"
          ipv4.dhcp-timeout: "15"
          ipv4.may-fail: "true"
YAML
if sudo cmp -s "$TMP_NETPLAN" "$NETPLAN_ETH_PATH" 2>/dev/null; then
    echo "  $NETPLAN_ETH_PATH already up to date."
else
    sudo install -m 600 -o root -g root "$TMP_NETPLAN" "$NETPLAN_ETH_PATH"
    echo "  Wrote $NETPLAN_ETH_PATH"
    CHANGES_MADE=true
fi
rm -f "$TMP_NETPLAN"

# Only `netplan apply` when something actually changed — it triggers a
# NetworkManager reconfigure that briefly bounces eth0 (and on some systems
# logs noisy systemd-networkd warnings even when we render via NM).
if [ "$CHANGES_MADE" = "true" ]; then
    echo
    echo "Applying netplan..."
    sudo netplan apply
fi

echo
echo "=== Done ==="
echo
echo "Verify with:"
echo "  ip -br addr show eth0              # static $ETH_STATIC_ADDR + DHCP"
echo "  iw dev wlan0 info                  # ssid $AP_SSID, type AP, ch $AP_CHANNEL"
echo "  sudo iptables-nft -L FORWARD -nv   # two REJECT rules with wlan0 in in/out columns"
echo "                                     # (use -nv; plain -n hides the iface columns)"
echo
echo "Join the AP from a client:"
echo "  SSID: $AP_SSID"
echo "  Password: $AP_PSK"
echo "  Pi reachable at $AP_ADDR (or http://racecar-neo.local)"
if [ "$CHANGES_MADE" = "false" ]; then
    echo
    echo "(No configuration changes were necessary — system already matched.)"
fi
