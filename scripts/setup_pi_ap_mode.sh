#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

AP_IFACE="wlan0"
UPLINK_IFACE="eth0"
AP_CIDR="10.13.37.1/24"
AP_SUBNET="10.13.37.0/24"
AP_IP="10.13.37.1"
DHCP_START="10.13.37.50"
DHCP_END="10.13.37.150"
COUNTRY_CODE="US"
CHANNEL="6"
SSID="RNS-Traveller"
PASSPHRASE=""
IP_BIN=""

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/setup_pi_ap_mode.sh --passphrase <pass> [options]

Required:
  --passphrase <text>      WPA2 passphrase (8-63 chars)

Optional:
  --ssid <name>            AP SSID (default: RNS-Traveller)
  --country <code>         ISO country code (default: US)
  --channel <n>            Wi-Fi channel (default: 6)
  --ap-iface <iface>       AP interface (default: wlan0)
  --uplink-iface <iface>   Uplink interface for internet sharing (default: eth0)
  --ap-cidr <cidr>         AP interface address (default: 10.13.37.1/24)
  --ap-subnet <cidr>       AP subnet for NAT rule (default: 10.13.37.0/24)
  --dhcp-start <ip>        DHCP range start (default: 10.13.37.50)
  --dhcp-end <ip>          DHCP range end (default: 10.13.37.150)
  --help                   Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --passphrase)
            PASSPHRASE="${2:-}"
            shift 2
            ;;
        --ssid)
            SSID="${2:-}"
            shift 2
            ;;
        --country)
            COUNTRY_CODE="${2:-}"
            shift 2
            ;;
        --channel)
            CHANNEL="${2:-}"
            shift 2
            ;;
        --ap-iface)
            AP_IFACE="${2:-}"
            shift 2
            ;;
        --uplink-iface)
            UPLINK_IFACE="${2:-}"
            shift 2
            ;;
        --ap-cidr)
            AP_CIDR="${2:-}"
            shift 2
            ;;
        --ap-subnet)
            AP_SUBNET="${2:-}"
            shift 2
            ;;
        --dhcp-start)
            DHCP_START="${2:-}"
            shift 2
            ;;
        --dhcp-end)
            DHCP_END="${2:-}"
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

if [ -z "${PASSPHRASE}" ]; then
    echo "--passphrase is required." >&2
    exit 2
fi

if [ "${#PASSPHRASE}" -lt 8 ] || [ "${#PASSPHRASE}" -gt 63 ]; then
    echo "Passphrase length must be 8-63 characters." >&2
    exit 2
fi

if [ "${#SSID}" -lt 1 ] || [ "${#SSID}" -gt 32 ]; then
    echo "SSID length must be 1-32 characters." >&2
    exit 2
fi

if [ ! -d "${REPO_DIR}/deploy/network" ]; then
    echo "deploy/network not found in repo." >&2
    exit 1
fi

AP_IP="${AP_CIDR%%/*}"

IP_BIN="$(command -v ip || true)"
if [ -z "${IP_BIN}" ]; then
    echo "ip command not found." >&2
    exit 1
fi

render_template() {
    local template_path="$1"
    local output_path="$2"
    sed \
        -e "s|{{AP_IFACE}}|${AP_IFACE}|g" \
        -e "s|{{UPLINK_IFACE}}|${UPLINK_IFACE}|g" \
        -e "s|{{AP_CIDR}}|${AP_CIDR}|g" \
        -e "s|{{AP_IP}}|${AP_IP}|g" \
        -e "s|{{AP_SUBNET}}|${AP_SUBNET}|g" \
        -e "s|{{DHCP_START}}|${DHCP_START}|g" \
        -e "s|{{DHCP_END}}|${DHCP_END}|g" \
        -e "s|{{COUNTRY_CODE}}|${COUNTRY_CODE}|g" \
        -e "s|{{CHANNEL}}|${CHANNEL}|g" \
        -e "s|{{SSID}}|${SSID}|g" \
        -e "s|{{PASSPHRASE}}|${PASSPHRASE}|g" \
        -e "s|{{IP_BIN}}|${IP_BIN}|g" \
        "${template_path}" > "${output_path}"
}

echo "[1/9] Installing required packages..."
apt-get update
apt-get install -y hostapd dnsmasq nftables

echo "[2/9] Writing hostapd config..."
mkdir -p /etc/hostapd
render_template \
    "${REPO_DIR}/deploy/network/hostapd.pi-rns-traveller.conf.template" \
    /etc/hostapd/hostapd.conf

if [ -f /etc/default/hostapd ]; then
    if grep -Eq '^\s*#?\s*DAEMON_CONF=' /etc/default/hostapd; then
        sed -i 's|^\s*#\?\s*DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd
    else
        echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' >> /etc/default/hostapd
    fi
else
    echo 'DAEMON_CONF="/etc/hostapd/hostapd.conf"' > /etc/default/hostapd
fi

echo "[3/9] Writing dnsmasq config..."
mkdir -p /etc/dnsmasq.d
render_template \
    "${REPO_DIR}/deploy/network/dnsmasq.pi-rns-traveller.conf.template" \
    /etc/dnsmasq.d/pi-rns-traveller.conf

echo "[4/9] Configuring static AP address in dhcpcd..."
if [ -f /etc/dhcpcd.conf ]; then
    tmp_dhcpcd="$(mktemp)"
    awk '
BEGIN {skip=0}
/^# BEGIN PI-RNS-TRAVELLER-AP$/ {skip=1; next}
/^# END PI-RNS-TRAVELLER-AP$/ {skip=0; next}
skip==0 {print}
' /etc/dhcpcd.conf > "${tmp_dhcpcd}"

    cat >> "${tmp_dhcpcd}" <<EOF

# BEGIN PI-RNS-TRAVELLER-AP
interface ${AP_IFACE}
    static ip_address=${AP_CIDR}
    nohook wpa_supplicant
# END PI-RNS-TRAVELLER-AP
EOF

    install -m 0644 "${tmp_dhcpcd}" /etc/dhcpcd.conf
    rm -f "${tmp_dhcpcd}"
else
    echo "dhcpcd.conf not found; continuing with systemd AP address service only."
fi

echo "[4b/9] Installing static AP address service..."
render_template \
    "${REPO_DIR}/deploy/network/pi-rns-ap-addr.service.template" \
    /etc/systemd/system/pi-rns-ap-addr.service

mkdir -p /etc/systemd/system/hostapd.service.d /etc/systemd/system/dnsmasq.service.d
cat > /etc/systemd/system/hostapd.service.d/pi-rns-ap.conf <<'EOF'
[Unit]
After=pi-rns-ap-addr.service
Wants=pi-rns-ap-addr.service
EOF
cat > /etc/systemd/system/dnsmasq.service.d/pi-rns-ap.conf <<'EOF'
[Unit]
After=pi-rns-ap-addr.service
Wants=pi-rns-ap-addr.service
EOF

echo "[5/9] Enabling IP forwarding..."
mkdir -p /etc/sysctl.d
cat > /etc/sysctl.d/99-pi-rns-traveller-ap.conf <<'EOF'
net.ipv4.ip_forward=1
EOF
sysctl --system >/dev/null

echo "[6/9] Writing nftables policy..."
mkdir -p /etc/nftables.d
render_template \
    "${REPO_DIR}/deploy/network/nftables.pi-rns-traveller-ap.nft.template" \
    /etc/nftables.d/pi-rns-traveller-ap.nft

if [ ! -f /etc/nftables.conf ]; then
    cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset
include "/etc/nftables.d/*.nft"
EOF
else
    if ! grep -Fq '/etc/nftables.d/*.nft' /etc/nftables.conf; then
        printf '\ninclude "/etc/nftables.d/*.nft"\n' >> /etc/nftables.conf
    fi
fi

echo "[7/9] Installing AP health-check timer..."
install -m 0755 "${REPO_DIR}/deploy/network/pi-rns-ap-healthcheck.sh" /usr/local/libexec/pi-rns-ap-healthcheck.sh
render_template \
    "${REPO_DIR}/deploy/network/pi-rns-ap-health.service.template" \
    /etc/systemd/system/pi-rns-ap-health.service
install -m 0644 "${REPO_DIR}/deploy/network/pi-rns-ap-health.timer" /etc/systemd/system/pi-rns-ap-health.timer

echo "[8/9] Enabling services..."
systemctl unmask hostapd || true
systemctl disable --now wpa_supplicant@"${AP_IFACE}".service || true
if systemctl list-unit-files | grep -q '^NetworkManager\.service'; then
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/99-pi-rns-traveller-unmanaged.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:${AP_IFACE}
EOF
    if systemctl is-active --quiet NetworkManager; then
        systemctl restart NetworkManager || true
    fi
fi
systemctl enable pi-rns-ap-addr.service hostapd dnsmasq nftables pi-rns-ap-health.timer

echo "[9/9] Restarting networking stack..."
systemctl daemon-reload
if systemctl list-unit-files | grep -q '^dhcpcd\.service'; then
    systemctl restart dhcpcd || true
fi
systemctl restart pi-rns-ap-addr.service
systemctl restart nftables
systemctl restart hostapd
systemctl restart dnsmasq
systemctl restart pi-rns-ap-health.timer

echo
echo "AP setup complete."
echo "SSID: ${SSID}"
echo "AP IP: ${AP_CIDR}"
echo "Uplink interface for internet sharing: ${UPLINK_IFACE}"
echo
echo "Next steps:"
echo "  1) Connect phone/laptop to SSID '${SSID}'."
echo "  2) SSH to: ssh jferris@${AP_CIDR%%/*}"
echo "  3) Trigger immediate traveller run: touch /tmp/pi-rns-traveller.run-now"
