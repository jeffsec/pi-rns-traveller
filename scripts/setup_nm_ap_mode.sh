#!/usr/bin/env bash
set -euo pipefail

AP_IFACE="wlan0"
ETH_IFACE="eth0"
AP_CONN="traveller-ap"
AP_SSID="RNS-Traveller"
AP_PSK=""
AP_CIDR="10.42.0.1/24"
ETH_DHCP_CONN="eth-dhcp"
ETH_DIRECT_CONN="eth-direct"
ETH_DIRECT_CIDR="192.168.77.1/24"

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/setup_nm_ap_mode.sh --passphrase <pass> [options]

Required:
  --passphrase <text>      AP WPA2 passphrase (8-63 chars)

Optional:
  --ssid <name>            AP SSID (default: RNS-Traveller)
  --ap-cidr <cidr>         AP gateway CIDR (default: 10.42.0.1/24)
  --ap-iface <iface>       AP interface (default: wlan0)
  --eth-iface <iface>      Ethernet interface (default: eth0)
  --eth-direct-cidr <cidr> Direct-cable fallback CIDR (default: 192.168.77.1/24)
  --help                   Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --passphrase)
            AP_PSK="${2:-}"
            shift 2
            ;;
        --ssid)
            AP_SSID="${2:-}"
            shift 2
            ;;
        --ap-cidr)
            AP_CIDR="${2:-}"
            shift 2
            ;;
        --ap-iface)
            AP_IFACE="${2:-}"
            shift 2
            ;;
        --eth-iface)
            ETH_IFACE="${2:-}"
            shift 2
            ;;
        --eth-direct-cidr)
            ETH_DIRECT_CIDR="${2:-}"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

if [ "${EUID}" -ne 0 ]; then
    echo "Run as root (use sudo)." >&2
    exit 1
fi

if [ -z "${AP_PSK}" ]; then
    echo "--passphrase is required." >&2
    exit 2
fi

if [ "${#AP_PSK}" -lt 8 ] || [ "${#AP_PSK}" -gt 63 ]; then
    echo "Passphrase length must be 8-63 characters." >&2
    exit 2
fi

echo "[1/7] Installing and enabling NetworkManager + SSH..."
apt-get update
apt-get install -y network-manager openssh-server
systemctl enable --now NetworkManager ssh

echo "[2/7] Disabling conflicting network stack/services..."
systemctl disable --now hostapd dnsmasq nftables pi-rns-ap-health.timer pi-rns-ap-addr.service 2>/dev/null || true
rm -f /etc/NetworkManager/conf.d/99-pi-rns-traveller-unmanaged.conf
systemctl disable --now wpa_supplicant@"${AP_IFACE}".service 2>/dev/null || true
systemctl disable --now dhcpcd 2>/dev/null || true

echo "[3/7] Setting Wi-Fi power save off for link stability..."
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-wifi-powersave-off.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF

echo "[4/7] Configuring AP profile (${AP_CONN})..."
if ! nmcli -t -f NAME con show | grep -Fxq "${AP_CONN}"; then
    nmcli con add type wifi ifname "${AP_IFACE}" con-name "${AP_CONN}" ssid "${AP_SSID}"
fi
nmcli con mod "${AP_CONN}" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.ssid "${AP_SSID}" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "${AP_PSK}" \
    ipv4.method shared \
    ipv4.addresses "${AP_CIDR}" \
    ipv6.method ignore \
    connection.autoconnect yes \
    connection.autoconnect-priority 100

echo "[5/7] Configuring Ethernet DHCP profile (${ETH_DHCP_CONN})..."
if ! nmcli -t -f NAME con show | grep -Fxq "${ETH_DHCP_CONN}"; then
    nmcli con add type ethernet ifname "${ETH_IFACE}" con-name "${ETH_DHCP_CONN}"
fi
nmcli con mod "${ETH_DHCP_CONN}" \
    ipv4.method auto \
    ipv6.method ignore \
    connection.autoconnect yes \
    connection.autoconnect-priority 90

echo "[6/7] Configuring direct-cable recovery Ethernet profile (${ETH_DIRECT_CONN})..."
if ! nmcli -t -f NAME con show | grep -Fxq "${ETH_DIRECT_CONN}"; then
    nmcli con add type ethernet ifname "${ETH_IFACE}" con-name "${ETH_DIRECT_CONN}"
fi
nmcli con mod "${ETH_DIRECT_CONN}" \
    ipv4.method manual \
    ipv4.addresses "${ETH_DIRECT_CIDR}" \
    ipv6.method ignore \
    connection.autoconnect no \
    connection.autoconnect-priority 10

echo "[7/7] Restarting NetworkManager and bringing up AP + Ethernet DHCP..."
systemctl restart NetworkManager
nmcli con up "${AP_CONN}" || true
nmcli con up "${ETH_DHCP_CONN}" || true

echo
echo "NetworkManager AP setup complete."
echo "AP SSID: ${AP_SSID}"
echo "AP gateway: ${AP_CIDR%%/*}"
echo "Ethernet DHCP profile: ${ETH_DHCP_CONN}"
echo "Ethernet direct fallback profile: ${ETH_DIRECT_CONN} (${ETH_DIRECT_CIDR})"
echo
echo "Recovery tips:"
echo "  - AP access: ssh jferris@${AP_CIDR%%/*}"
echo "  - Direct cable fallback: nmcli con up ${ETH_DIRECT_CONN}"
echo "    then set laptop to 192.168.77.2/24 and ssh to 192.168.77.1"
echo "  - Return to DHCP: nmcli con up ${ETH_DHCP_CONN}"

