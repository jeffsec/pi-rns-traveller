# pi-rns-traveller

Portable RNode connectivity check script for Pi Zero 2W field use.

## What It Does

- Detects likely RNode serial port (`/dev/ttyACM*`, `/dev/ttyUSB*`, `/dev/serial/by-id/*`).
- Clones your Reticulum config into a temp runtime directory.
- Patches RNode/KISS interface `port = ...` to the detected serial device.
- Starts `rnsd` using that runtime config.
- Runs `rnprobe` against a list of targets.
- Prints a compact SSH-friendly summary.

Port selection precedence:

1. `--port` (explicit override)
2. Existing `port = ...` in your Reticulum serial interface config
3. Auto-detection

## Setup

1. Create targets file:

```bash
cp config/targets.example.txt config/targets.txt
```

2. Edit `config/targets.txt` with your destination hashes.

## Run

```bash
python3 scripts/traveller_probe.py --base-config-dir ~/.reticulum --targets-file config/targets.txt
```

Useful flags:

- `--list-ports` list detected serial devices.
- `--port /dev/ttyACM0` force a specific port.
- `--timeout 10 --probes 1` tune probing.
- `--heartbeat-seconds 3` print progress dots while each probe runs (helps SSH sessions stay alive).
- `--no-progress` disable per-target progress output.
- `--keep-runtime` keep generated runtime config/log for debugging.
- `--verbose` print raw `rnprobe` output per target.
- `--gpsd` try reading a GPS fix from local `gpsd` (if a GPS receiver is attached).
- `--lat ... --lon ... [--alt ...]` manually attach coordinates when no GPS hardware is present.
- `--ups-hat-c` read battery metrics from Waveshare UPS HAT (C) INA219 (default addr `0x43`).
- `--ups-i2c-bus 1 --ups-i2c-addr 0x43` override UPS HAT (C) I2C bus/address.
- `--history-file logs/traveller-history.csv` append per-target run rows (location + outcome).
- `--no-history` disable CSV history logging.

## Important Config Note

The script expects your base Reticulum config to already contain at least one serial interface block with:

- `type = RNodeInterface`, `KISSInterface`, or `AX25KISSInterface`.

If none are present, patching fails. In that case, add one in `~/.reticulum/config` first.
