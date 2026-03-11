#!/usr/bin/env bash
set -euo pipefail

AP_IFACE="${1:-wlan0}"
AP_CIDR="${2:-10.13.37.1/24}"

log() {
    logger -t pi-rns-ap-health "$*"
    echo "pi-rns-ap-health: $*"
}

service_restart_needed=0

if ! systemctl is-active --quiet hostapd; then
    log "hostapd inactive"
    service_restart_needed=1
fi

if ! systemctl is-active --quiet dnsmasq; then
    log "dnsmasq inactive"
    service_restart_needed=1
fi

if ! ip -4 addr show dev "${AP_IFACE}" | grep -Fq "inet ${AP_CIDR}"; then
    log "missing AP address ${AP_CIDR} on ${AP_IFACE}"
    service_restart_needed=1
fi

if [ "${service_restart_needed}" -eq 1 ]; then
    log "restarting dhcpcd, hostapd, and dnsmasq"
    systemctl restart dhcpcd || true
    sleep 2
    systemctl restart hostapd dnsmasq
fi

if ! systemctl is-active --quiet nftables; then
    log "nftables inactive, restarting"
    systemctl restart nftables
fi

exit 0

