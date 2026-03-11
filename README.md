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
cp config/targets.example.txt config/targets.local.txt
```

2. Edit `config/targets.local.txt` with your destination hashes.

Private/local target list (recommended):

- Put personal hashes in `config/targets.local.txt` (ignored by git).
- If `config/targets.local.txt` exists, scripts use it automatically.
- You can still override with `--targets-file ...`.

## Run

```bash
python3 scripts/traveller_probe.py --base-config-dir ~/.reticulum
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

## Appliance Mode (Boot -> Check -> Results)

`scripts/traveller_appliance.py` is the always-on controller for your Pi + UPS + ePaper build.

What it does:

- Shows `BOOTING`, `CHECKING`, `RESULTS`, and `ERROR` states.
- Uses a full-screen dashboard layout with per-target status tiles.
- Tile details are compact by design (`PROBE`, `1.9s`, `TIMEOUT`, `FAIL`) for readability in small boxes.
- Runs periodic probe cycles without SSH interaction.
- Writes durable logs to SQLite with `WAL` + `synchronous=FULL`.
- Writes last known state atomically to `state/state.json`.
- Holds on `ERROR` until restart by default.
- Prints detailed per-target progress and a full run summary to the console.

Run once for testing:

```bash
python3 scripts/traveller_appliance.py --once --no-epd
```

Run with ePaper + battery + GPS:

```bash
python3 scripts/traveller_appliance.py \
  --ups-hat-c \
  --gpsd \
  --check-interval-seconds 120
```

Important flags:

- `--no-epd` console-only mode.
- `--epd-driver auto|epd2in13_V3|epd2in13_V2|epd2in13` force panel driver.
- `--epd-partial-every 5` force a full refresh after N partial refreshes (ghosting control).
- `--check-interval-seconds 120` periodic run interval (default: 120s).
- `--trigger-file /tmp/pi-rns-traveller.run-now` touch-file path for immediate run.
- `--state-dir /path` persistent state/log directory.
- `--continue-on-error` continue periodic checks after failures.

Display refresh behavior:

- `CHECKING` and `WAIT` updates use partial refresh when supported.
- `BOOTING`, `RESULTS`, and `ERROR` use full refresh.
- State transitions force full refresh, and full refresh is also forced every `--epd-partial-every` partial updates.

Durable appliance logs:

- `state/history.db` SQLite run + per-target result history.
- `state/state.json` latest appliance screen state.

Manual run trigger while service is waiting:

```bash
touch /tmp/pi-rns-traveller.run-now
```

## Systemd Autostart

Service template:

- `deploy/pi-rns-traveller.service`

Install on Pi:

```bash
sudo cp deploy/pi-rns-traveller.service /etc/systemd/system/pi-rns-traveller.service
sudo systemctl daemon-reload
sudo systemctl enable pi-rns-traveller.service
sudo systemctl start pi-rns-traveller.service
```

Check status:

```bash
systemctl status pi-rns-traveller.service --no-pager
journalctl -u pi-rns-traveller.service -n 120 --no-pager
```

Change interval without editing repo files:

```bash
sudo systemctl edit pi-rns-traveller.service
```

Add:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /home/jferris/pi-rns-traveller/scripts/traveller_appliance.py --base-config-dir /home/jferris/.reticulum --ups-hat-c --gpsd --state-dir /home/jferris/pi-rns-traveller/state --check-interval-seconds 300 --trigger-file /tmp/pi-rns-traveller.run-now
```

Then apply:

```bash
sudo systemctl daemon-reload
sudo systemctl restart pi-rns-traveller.service
```

## Field Networking (Always-On AP)

For trail reliability, keep `wlan0` in AP mode all the time and use `eth0` (USB Ethernet dongle) for internet at home.

What this setup installs:

- `hostapd` AP on `wlan0`
- `dnsmasq` DHCP/DNS for AP clients
- `nftables` firewall + NAT from AP subnet to `eth0`
- AP health-check timer (`pi-rns-ap-health.timer`) that self-heals hostapd/dnsmasq failures

One-time setup on the Pi:

```bash
cd ~/pi-rns-traveller
chmod +x scripts/setup_pi_ap_mode.sh
sudo ./scripts/setup_pi_ap_mode.sh \
  --ssid "RNS-Traveller" \
  --passphrase "replace-with-strong-passphrase"
```

Then install/refresh traveller appliance service:

```bash
sudo cp deploy/pi-rns-traveller.service /etc/systemd/system/pi-rns-traveller.service
sudo systemctl daemon-reload
sudo systemctl enable --now pi-rns-traveller.service
```

Verify:

```bash
ip -4 addr show wlan0
systemctl status hostapd dnsmasq nftables pi-rns-ap-health.timer pi-rns-traveller.service --no-pager
journalctl -u pi-rns-ap-health.service -n 60 --no-pager
```

Expected defaults:

- AP IP: `10.13.37.1/24`
- DHCP clients: `10.13.37.50-10.13.37.150`
- Manual run trigger: `touch /tmp/pi-rns-traveller.run-now`

Home/dev behavior:

- Keep AP running as normal.
- Plug in Ethernet dongle to `eth0` for internet (updates/package installs/maintenance).
- AP clients can use that uplink through NAT automatically.
