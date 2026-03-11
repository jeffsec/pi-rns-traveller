#!/usr/bin/env bash
set -euo pipefail

PANEL_DRIVER="epd2in13_V4"
ENABLE_INTERFACES=1
TARGET_USER="${SUDO_USER:-${USER}}"

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/setup_epaper_v4.sh [options]

Options:
  --target-user <user>     User that owns ~/e-Paper (default: invoking user)
  --panel-driver <name>    Driver import name (default: epd2in13_V4)
  --skip-interfaces        Do not run raspi-config SPI/I2C enable
  --help                   Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --target-user)
            TARGET_USER="${2:-}"
            shift 2
            ;;
        --panel-driver)
            PANEL_DRIVER="${2:-}"
            shift 2
            ;;
        --skip-interfaces)
            ENABLE_INTERFACES=0
            shift
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

if ! id "${TARGET_USER}" >/dev/null 2>&1; then
    echo "Target user '${TARGET_USER}' not found." >&2
    exit 2
fi

TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
if [ -z "${TARGET_HOME}" ]; then
    echo "Could not resolve home for user '${TARGET_USER}'." >&2
    exit 2
fi

EPAPER_ROOT="${TARGET_HOME}/e-Paper"
EPAPER_LIB="${EPAPER_ROOT}/RaspberryPi_JetsonNano/python/lib"
EPAPER_DRIVER_FILE="${EPAPER_LIB}/waveshare_epd/${PANEL_DRIVER}.py"

echo "[1/4] Installing ePaper + UPS dependencies..."
apt-get update
apt-get install -y \
    git \
    i2c-tools \
    python3 \
    python3-numpy \
    python3-pil \
    python3-pip \
    python3-rpi.gpio \
    python3-smbus \
    python3-spidev

if [ "${ENABLE_INTERFACES}" -eq 1 ]; then
    echo "[2/4] Enabling SPI/I2C via raspi-config..."
    if command -v raspi-config >/dev/null 2>&1; then
        raspi-config nonint do_spi 0
        raspi-config nonint do_i2c 0
    else
        echo "raspi-config not found; enable SPI/I2C manually."
    fi
else
    echo "[2/4] Skipping SPI/I2C interface enable."
fi

echo "[3/4] Installing/updating Waveshare e-Paper repo at ${EPAPER_ROOT}..."
if [ -d "${EPAPER_ROOT}/.git" ]; then
    sudo -u "${TARGET_USER}" git -C "${EPAPER_ROOT}" pull --ff-only
else
    sudo -u "${TARGET_USER}" git clone https://github.com/waveshareteam/e-Paper.git "${EPAPER_ROOT}"
fi

if [ ! -f "${EPAPER_DRIVER_FILE}" ]; then
    echo "Driver file not found: ${EPAPER_DRIVER_FILE}" >&2
    exit 1
fi

echo "[4/4] Verifying Python driver import (${PANEL_DRIVER})..."
sudo -u "${TARGET_USER}" env "PYTHONPATH=${EPAPER_LIB}" \
    python3 -c "from waveshare_epd import ${PANEL_DRIVER}; print('import ok: ${PANEL_DRIVER}')"

echo
echo "ePaper setup complete."
echo "WaveShare library path: ${EPAPER_LIB}"
echo "Panel driver: ${PANEL_DRIVER}"
echo
echo "Recommended next checks:"
echo "  ls /dev/spidev0.0"
echo "  i2cdetect -y 1"
echo
echo "If SPI/I2C were just enabled, reboot before running traveller appliance."

