#!/usr/bin/env python3
"""
Portable RNode reachability check for Pi Zero + UPS builds.

Workflow:
1) Detect the most likely serial device for RNode/KISS.
2) Clone Reticulum config into a temp runtime dir.
3) Patch serial interface port to detected device.
4) Start rnsd with the runtime config.
5) Run rnprobe checks against configured targets.
6) Print a compact console summary.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SERIAL_TYPE_NAMES = {"rnodeinterface", "kissinterface", "ax25kissinterface"}
TYPE_RE = re.compile(r"^(\s*)type\s*=\s*([A-Za-z0-9_]+)\s*$")
PORT_RE = re.compile(r"^(\s*)port\s*=.*$")
ENABLED_RE = re.compile(r"^(\s*)enabled\s*=.*$")
HEADER_RE = re.compile(r"^\s*\[\[.*\]\]\s*$")
RETICULUM_SECTION_RE = re.compile(r"^\s*\[reticulum\]\s*$", re.IGNORECASE)
ANY_SECTION_RE = re.compile(r"^\s*\[[^\[].*\]\s*$")
INSTANCE_NAME_RE = re.compile(r"^(\s*)instance_name\s*=.*$", re.IGNORECASE)
SHARE_INSTANCE_RE = re.compile(r"^(\s*)share_instance\s*=.*$", re.IGNORECASE)
SENT_RECV_RE = re.compile(r"Sent\s+(\d+),\s+received\s+(\d+),\s+packet\s+loss\s+([0-9.]+)%")
RTT_RE = re.compile(
    r"Round-trip time is\s+([0-9.]+)\s+(milliseconds|seconds)\s+over\s+(\d+)\s+hop",
    re.IGNORECASE,
)
RSSI_RE = re.compile(r"(?:\[)?RSSI\s+(-?[0-9]+(?:\.[0-9]+)?)\s*dBm(?:\])?", re.IGNORECASE)
SNR_RE = re.compile(r"(?:\[)?SNR\s+(-?[0-9]+(?:\.[0-9]+)?)\s*dB(?:\])?", re.IGNORECASE)
LINK_QUALITY_RE = re.compile(
    r"(?:\[)?Link\s+Quality\s+([0-9]+(?:\.[0-9]+)?)%(?:\])?",
    re.IGNORECASE,
)
HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
DEFAULT_TARGETS_FILE = "config/targets.txt"
LOCAL_TARGETS_FILE = Path("config/targets.local.txt")
INTERFACE_HEADER_RE = re.compile(r"^\s*\[\[(.+?)\]\]\s*$")


@dataclass
class Target:
    label: str
    full_name: str
    destination_hash: str


@dataclass
class ProbeResult:
    target: Target
    sent: int
    received: int
    loss_pct: float
    rtt_ms: float | None
    hops: int | None
    rssi_dbm: float | None
    snr_db: float | None
    link_quality_pct: float | None
    reachable: bool
    reason: str
    exit_code: int


@dataclass
class LocationFix:
    lat: float | None
    lon: float | None
    alt_m: float | None
    source: str


@dataclass
class BatteryStatus:
    voltage_v: float | None
    current_a: float | None
    power_w: float | None
    percent: float | None
    source: str
    error: str | None = None


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def status(message: str) -> None:
    print(message, flush=True)


def command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def get_gpsd_fix(timeout_seconds: float, host: str = "127.0.0.1", port: int = 2947) -> LocationFix:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    try:
        with socket.create_connection((host, port), timeout=2.0) as sock:
            sock.settimeout(1.0)
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buffer = ""
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="ignore")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or '"class":"TPV"' not in line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    mode = int(payload.get("mode", 0) or 0)
                    lat = _to_float(payload.get("lat"))
                    lon = _to_float(payload.get("lon"))
                    alt = _to_float(payload.get("alt"))
                    if mode >= 2 and lat is not None and lon is not None:
                        return LocationFix(lat=lat, lon=lon, alt_m=alt, source="gpsd")
    except OSError:
        pass

    return LocationFix(lat=None, lon=None, alt_m=None, source="gps-none")


def resolve_location(
    manual_lat: float | None,
    manual_lon: float | None,
    manual_alt: float | None,
    use_gpsd: bool,
    gpsd_timeout: float,
) -> LocationFix:
    if manual_lat is not None or manual_lon is not None:
        if manual_lat is None or manual_lon is None:
            raise RuntimeError("Both --lat and --lon must be set together.")
        return LocationFix(lat=manual_lat, lon=manual_lon, alt_m=manual_alt, source="manual")

    if use_gpsd:
        return get_gpsd_fix(timeout_seconds=gpsd_timeout)

    return LocationFix(lat=None, lon=None, alt_m=None, source="none")


def read_ups_hat_c_status(i2c_bus: int = 1, addr: int = 0x43) -> BatteryStatus:
    # Waveshare UPS HAT (C) uses INA219 at 0x43 and this calibration profile.
    reg_config = 0x00
    reg_shunt = 0x01
    reg_bus = 0x02
    reg_power = 0x03
    reg_current = 0x04
    reg_cal = 0x05

    cal_value = 26868
    current_lsb = 0.1524
    power_lsb = 0.003048
    config_value = 0x199F

    try:
        import smbus  # type: ignore
    except Exception as exc:
        return BatteryStatus(
            voltage_v=None,
            current_a=None,
            power_w=None,
            percent=None,
            source="ups-hat-c-unavailable",
            error=f"smbus import failed: {exc}",
        )

    try:
        bus = smbus.SMBus(i2c_bus)

        def read_word(register: int) -> int:
            data = bus.read_i2c_block_data(addr, register, 2)
            return (data[0] << 8) | data[1]

        def write_word(register: int, value: int) -> None:
            payload = [(value >> 8) & 0xFF, value & 0xFF]
            bus.write_i2c_block_data(addr, register, payload)

        write_word(reg_cal, cal_value)
        write_word(reg_config, config_value)
        time.sleep(0.05)

        write_word(reg_cal, cal_value)
        bus_voltage_v = (read_word(reg_bus) >> 3) * 0.004

        write_word(reg_cal, cal_value)
        shunt_raw = read_word(reg_shunt)
        if shunt_raw > 32767:
            shunt_raw -= 65535
        shunt_v = (shunt_raw * 0.01) / 1000.0

        current_raw = read_word(reg_current)
        if current_raw > 32767:
            current_raw -= 65535
        current_a = (current_raw * current_lsb) / 1000.0

        write_word(reg_cal, cal_value)
        power_raw = read_word(reg_power)
        if power_raw > 32767:
            power_raw -= 65535
        power_w = power_raw * power_lsb

        # Waveshare reference formula: p = (bus_voltage - 3) / 1.2 * 100
        percent = ((bus_voltage_v - 3.0) / 1.2) * 100.0
        percent = 100.0 if percent > 100.0 else percent
        percent = 0.0 if percent < 0.0 else percent

        # PSU/load voltage estimate from Waveshare note.
        load_v = bus_voltage_v + shunt_v

        try:
            bus.close()
        except Exception:
            pass

        return BatteryStatus(
            voltage_v=load_v,
            current_a=current_a,
            power_w=power_w,
            percent=percent,
            source="ups-hat-c",
        )
    except Exception as exc:
        return BatteryStatus(
            voltage_v=None,
            current_a=None,
            power_w=None,
            percent=None,
            source="ups-hat-c-error",
            error=str(exc),
        )


def append_history_rows(
    history_file: Path,
    run_started_utc: dt.datetime,
    elapsed_s: float,
    port: str,
    location: LocationFix,
    battery: BatteryStatus,
    results: Iterable[ProbeResult],
) -> int:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    existed = history_file.exists()

    fieldnames = [
        "run_started_utc",
        "elapsed_s",
        "serial_port",
        "gps_source",
        "lat",
        "lon",
        "alt_m",
        "battery_source",
        "battery_voltage_v",
        "battery_current_a",
        "battery_power_w",
        "battery_pct",
        "battery_error",
        "label",
        "destination_hash",
        "reachable",
        "sent",
        "received",
        "loss_pct",
        "rtt_ms",
        "hops",
        "rssi_dbm",
        "snr_db",
        "link_quality_pct",
        "reason",
        "exit_code",
    ]

    rows = 0
    with history_file.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not existed:
            writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "run_started_utc": run_started_utc.replace(microsecond=0).isoformat(),
                    "elapsed_s": f"{elapsed_s:.3f}",
                    "serial_port": port,
                    "gps_source": location.source,
                    "lat": "" if location.lat is None else f"{location.lat:.7f}",
                    "lon": "" if location.lon is None else f"{location.lon:.7f}",
                    "alt_m": "" if location.alt_m is None else f"{location.alt_m:.2f}",
                    "battery_source": battery.source,
                    "battery_voltage_v": "" if battery.voltage_v is None else f"{battery.voltage_v:.3f}",
                    "battery_current_a": "" if battery.current_a is None else f"{battery.current_a:.4f}",
                    "battery_power_w": "" if battery.power_w is None else f"{battery.power_w:.4f}",
                    "battery_pct": "" if battery.percent is None else f"{battery.percent:.1f}",
                    "battery_error": "" if battery.error is None else battery.error,
                    "label": result.target.label,
                    "destination_hash": result.target.destination_hash,
                    "reachable": int(result.reachable),
                    "sent": result.sent,
                    "received": result.received,
                    "loss_pct": f"{result.loss_pct:.2f}",
                    "rtt_ms": "" if result.rtt_ms is None else f"{result.rtt_ms:.3f}",
                    "hops": "" if result.hops is None else result.hops,
                    "rssi_dbm": "" if result.rssi_dbm is None else f"{result.rssi_dbm:.1f}",
                    "snr_db": "" if result.snr_db is None else f"{result.snr_db:.2f}",
                    "link_quality_pct": "" if result.link_quality_pct is None else f"{result.link_quality_pct:.1f}",
                    "reason": result.reason,
                    "exit_code": result.exit_code,
                }
            )
            rows += 1

    return rows


def list_serial_candidates() -> list[str]:
    patterns = [
        "/dev/serial/by-id/*",
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
        "/dev/ttyAMA*",
        "/dev/ttyS*",
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
    ]

    candidates: list[str] = []
    for pattern in patterns:
        for path in sorted(Path("/").glob(pattern.lstrip("/"))):
            if path.exists():
                candidates.append(str(path))

    # De-duplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def serial_score(path: str) -> tuple[int, str]:
    lower = path.lower()
    score = 0
    if "/dev/serial/by-id/" in lower:
        score += 50
    if "rnode" in lower:
        score += 40
    if "heltec" in lower or "lora" in lower:
        score += 25
    if "ttyacm" in lower:
        score += 12
    if "ttyusb" in lower:
        score += 9
    if "usbmodem" in lower:
        score += 8
    return score, path


def detect_serial_port(explicit_port: str | None) -> tuple[str, list[str]]:
    candidates = list_serial_candidates()
    if explicit_port:
        return explicit_port, candidates

    if not candidates:
        raise RuntimeError("No serial candidates found under /dev (ttyACM/ttyUSB/etc).")

    chosen = sorted(candidates, key=serial_score, reverse=True)[0]
    return chosen, candidates


def ensure_runtime_config(base_config_dir: Path, runtime_dir: Path) -> Path:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    source_cfg = base_config_dir / "config"

    if source_cfg.exists():
        # Copy full Reticulum directory so identities/storage are available.
        shutil.copytree(base_config_dir, runtime_dir, dirs_exist_ok=True)
    else:
        result = subprocess.run(
            ["rnsd", "--exampleconfig"],
            check=True,
            capture_output=True,
            text=True,
        )
        (runtime_dir / "config").write_text(result.stdout, encoding="utf-8")

    runtime_cfg = runtime_dir / "config"
    if not runtime_cfg.exists():
        raise RuntimeError(f"Runtime config not found at {runtime_cfg}")
    return runtime_cfg


def extract_configured_serial_port(config_path: Path) -> str | None:
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=False)
    current_block: list[str] | None = None

    def _read_block_port(block: list[str]) -> str | None:
        iface_type = None
        for line in block:
            match = TYPE_RE.match(line)
            if match:
                iface_type = match.group(2).strip().lower()
                break

        if iface_type not in SERIAL_TYPE_NAMES:
            return None

        for line in block:
            port_match = PORT_RE.match(line)
            if port_match:
                value = line.split("=", 1)[1].strip()
                if value and value != "__SERIAL_PORT__":
                    return value
        return None

    for line in lines:
        if HEADER_RE.match(line):
            if current_block is not None:
                port = _read_block_port(current_block)
                if port:
                    return port
            current_block = [line]
            continue

        if current_block is not None:
            current_block.append(line)

    if current_block is not None:
        port = _read_block_port(current_block)
        if port:
            return port

    return None


def parse_boolish(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    lowered = value.strip().strip("\"'").lower()
    if lowered in {"yes", "true", "1", "on"}:
        return True
    if lowered in {"no", "false", "0", "off"}:
        return False
    return default


def _parse_interface_block_name(header_line: str) -> str | None:
    match = INTERFACE_HEADER_RE.match(header_line)
    if not match:
        return None
    return match.group(1).strip()


def enabled_serial_interface_names(config_path: Path) -> list[str]:
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=False)
    current_block_name: str | None = None
    current_block_lines: list[str] = []
    names: list[str] = []

    def process_block(block_name: str | None, block_lines: list[str]) -> None:
        if not block_name:
            return
        iface_type: str | None = None
        enabled_value: str | None = None
        for line in block_lines:
            type_match = TYPE_RE.match(line)
            if type_match:
                iface_type = type_match.group(2).strip().lower()
                continue
            enabled_match = ENABLED_RE.match(line)
            if enabled_match:
                enabled_value = line.split("=", 1)[1].strip()
        if iface_type in SERIAL_TYPE_NAMES and parse_boolish(enabled_value, default=True):
            names.append(block_name)

    for line in lines:
        block_name = _parse_interface_block_name(line)
        if block_name is not None:
            process_block(current_block_name, current_block_lines)
            current_block_name = block_name
            current_block_lines = []
        elif current_block_name is not None:
            current_block_lines.append(line)

    process_block(current_block_name, current_block_lines)
    return names


def read_rnstatus_json(config_dir: Path, timeout_seconds: float) -> dict[str, Any] | None:
    command_timeout = max(timeout_seconds, 0.2)
    try:
        run = subprocess.run(  # noqa: S603
            ["rnstatus", "--json", "--all", "--config", str(config_dir)],
            capture_output=True,
            text=True,
            timeout=command_timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if run.returncode != 0:
        return None

    payload = (run.stdout or "").strip()
    if not payload:
        return None

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def serial_interfaces_ready(
    stats: dict[str, Any],
    expected_names: list[str],
) -> tuple[bool, str]:
    interfaces = stats.get("interfaces")
    if not isinstance(interfaces, list):
        return False, "rnstatus did not return interface stats"

    serial_ifaces: list[dict[str, Any]] = []
    for interface in interfaces:
        if not isinstance(interface, dict):
            continue
        iface_type = str(interface.get("type", "")).lower()
        if iface_type in SERIAL_TYPE_NAMES:
            serial_ifaces.append(interface)

    if expected_names:
        expected = {name.lower() for name in expected_names}
        filtered = [
            interface
            for interface in serial_ifaces
            if str(interface.get("short_name", "")).lower() in expected
            or str(interface.get("name", "")).lower() in expected
        ]
        if filtered:
            serial_ifaces = filtered

    if not serial_ifaces:
        return False, "serial interface not listed yet"

    online = [interface for interface in serial_ifaces if bool(interface.get("status"))]
    if online:
        names = ", ".join(str(interface.get("short_name", "?")) for interface in online)
        return True, f"online: {names}"

    names = ", ".join(str(interface.get("short_name", "?")) for interface in serial_ifaces)
    return False, f"offline: {names}"


def wait_for_rnsd_ready(
    config_dir: Path,
    runtime_cfg: Path,
    rnsd_proc: subprocess.Popen[str],
    startup_seconds: float,
    ready_timeout_seconds: float,
    ready_poll_seconds: float,
) -> tuple[bool, str, float]:
    started = time.monotonic()

    startup_wait = max(startup_seconds, 0.0)
    if startup_wait > 0:
        time.sleep(startup_wait)

    if rnsd_proc.poll() is not None:
        waited = time.monotonic() - started
        return False, "rnsd exited during startup wait", waited

    if not command_exists("rnstatus"):
        waited = time.monotonic() - started
        return True, "rnstatus unavailable; skipped readiness gate", waited

    ready_timeout = max(ready_timeout_seconds, 0.0)
    if ready_timeout == 0:
        waited = time.monotonic() - started
        return True, "readiness timeout disabled", waited

    poll_seconds = max(ready_poll_seconds, 0.1)
    expected_serial_names = enabled_serial_interface_names(runtime_cfg)
    deadline = time.monotonic() + ready_timeout
    last_detail = "waiting for rnstatus"

    while time.monotonic() < deadline:
        if rnsd_proc.poll() is not None:
            waited = time.monotonic() - started
            return False, "rnsd exited before readiness", waited

        stats = read_rnstatus_json(config_dir, timeout_seconds=poll_seconds + 0.8)
        if stats is not None:
            ready, detail = serial_interfaces_ready(stats, expected_serial_names)
            last_detail = detail
            if ready:
                waited = time.monotonic() - started
                return True, detail, waited
        else:
            last_detail = "rnstatus not ready"

        time.sleep(poll_seconds)

    waited = time.monotonic() - started
    return False, f"timeout waiting for serial interface readiness ({last_detail})", waited


def process_interface_block(block: list[str], serial_port: str) -> tuple[list[str], bool]:
    iface_type = None
    for line in block:
        match = TYPE_RE.match(line)
        if match:
            iface_type = match.group(2).strip().lower()
            break

    if iface_type not in SERIAL_TYPE_NAMES:
        return block, False

    patched = False
    saw_port = False
    saw_enabled = False
    type_line_index: int | None = None
    type_indent = "    "
    new_block: list[str] = []

    for idx, line in enumerate(block):
        type_match = TYPE_RE.match(line)
        if type_match:
            type_line_index = len(new_block)
            type_indent = type_match.group(1) or "    "
            new_block.append(line)
            continue

        port_match = PORT_RE.match(line)
        if port_match:
            new_block.append(f"{port_match.group(1)}port = {serial_port}\n")
            saw_port = True
            patched = True
            continue

        enabled_match = ENABLED_RE.match(line)
        if enabled_match:
            new_block.append(f"{enabled_match.group(1)}enabled = yes\n")
            saw_enabled = True
            patched = True
            continue

        new_block.append(line)

    insert_pos = (type_line_index + 1) if type_line_index is not None else 1
    if not saw_port:
        new_block.insert(insert_pos, f"{type_indent}port = {serial_port}\n")
        insert_pos += 1
        patched = True

    if not saw_enabled:
        new_block.insert(insert_pos, f"{type_indent}enabled = yes\n")
        patched = True

    return new_block, patched


def patch_reticulum_settings(lines: list[str], instance_name: str) -> tuple[list[str], bool]:
    in_reticulum = False
    saw_instance_name = False
    saw_share_instance = False
    changed = False
    output: list[str] = []
    insert_index: int | None = None
    indent_for_insert = ""

    def flush_reticulum_insertions() -> None:
        nonlocal saw_instance_name, saw_share_instance, changed, insert_index
        if insert_index is None:
            return
        additions: list[str] = []
        if not saw_share_instance:
            additions.append(f"{indent_for_insert}share_instance = yes\n")
        if not saw_instance_name:
            additions.append(f"{indent_for_insert}instance_name = {instance_name}\n")
        if additions:
            output[insert_index:insert_index] = additions
            changed = True
        insert_index = None

    for line in lines:
        if RETICULUM_SECTION_RE.match(line):
            in_reticulum = True
            saw_instance_name = False
            saw_share_instance = False
            indent_for_insert = ""
            output.append(line)
            insert_index = len(output)
            continue

        if in_reticulum and ANY_SECTION_RE.match(line):
            flush_reticulum_insertions()
            in_reticulum = False

        if in_reticulum:
            instance_match = INSTANCE_NAME_RE.match(line)
            if instance_match:
                output.append(f"{instance_match.group(1)}instance_name = {instance_name}\n")
                saw_instance_name = True
                changed = True
                continue

            share_match = SHARE_INSTANCE_RE.match(line)
            if share_match:
                output.append(f"{share_match.group(1)}share_instance = yes\n")
                saw_share_instance = True
                changed = True
                continue

            if line.strip() and not line.lstrip().startswith("#"):
                indent_for_insert = re.match(r"^(\s*)", line).group(1)

        output.append(line)

    if in_reticulum:
        flush_reticulum_insertions()

    return output, changed


def patch_config(config_path: Path, serial_port: str, instance_name: str) -> bool:
    text = config_path.read_text(encoding="utf-8")

    if "__SERIAL_PORT__" in text:
        text = text.replace("__SERIAL_PORT__", serial_port)
        lines = text.splitlines(keepends=True)
        lines, _ = patch_reticulum_settings(lines, instance_name)
        config_path.write_text("".join(lines), encoding="utf-8")
        return True

    lines = text.splitlines(keepends=True)

    patched_interfaces = False
    patched_lines: list[str] = []
    current_block: list[str] | None = None

    for line in lines:
        if HEADER_RE.match(line):
            if current_block is not None:
                new_block, did_patch = process_interface_block(current_block, serial_port)
                patched_lines.extend(new_block)
                patched_interfaces = patched_interfaces or did_patch
            current_block = [line]
            continue

        if current_block is None:
            patched_lines.append(line)
        else:
            current_block.append(line)

    if current_block is not None:
        new_block, did_patch = process_interface_block(current_block, serial_port)
        patched_lines.extend(new_block)
        patched_interfaces = patched_interfaces or did_patch

    patched_lines, _ = patch_reticulum_settings(patched_lines, instance_name)

    if patched_interfaces:
        config_path.write_text("".join(patched_lines), encoding="utf-8")

    return patched_interfaces


def normalize_probe_output(output: str) -> str:
    # Remove backspace spinner artifacts and carriage-return updates.
    output = re.sub(r"\x08.", "", output)
    output = output.replace("\r", "\n")
    return output


def parse_probe_result(target: Target, output: str, exit_code: int) -> ProbeResult:
    clean = normalize_probe_output(output)
    sent = 0
    received = 0
    loss = 100.0
    rtt_ms: float | None = None
    hops: int | None = None
    rssi_dbm: float | None = None
    snr_db: float | None = None
    link_quality_pct: float | None = None
    reason = "unknown"

    sent_match = SENT_RECV_RE.search(clean)
    if sent_match:
        sent = int(sent_match.group(1))
        received = int(sent_match.group(2))
        loss = float(sent_match.group(3))

    rtt_match = RTT_RE.search(clean)
    if rtt_match:
        rtt_value = float(rtt_match.group(1))
        unit = rtt_match.group(2).lower()
        hops = int(rtt_match.group(3))
        rtt_ms = rtt_value * 1000 if "second" in unit else rtt_value

    rssi_match = RSSI_RE.search(clean)
    if rssi_match:
        rssi_dbm = float(rssi_match.group(1))

    snr_match = SNR_RE.search(clean)
    if snr_match:
        snr_db = float(snr_match.group(1))

    lq_match = LINK_QUALITY_RE.search(clean)
    if lq_match:
        link_quality_pct = float(lq_match.group(1))

    lower = clean.lower()
    if received > 0:
        reason = "ok"
    elif "path request timed out" in lower:
        reason = "path-timeout"
    elif "probe timed out" in lower:
        reason = "probe-timeout"
    elif "could not open serial port" in lower:
        reason = "serial-error"
    elif "operation not permitted" in lower:
        reason = "permission"
    elif "invalid destination" in lower:
        reason = "bad-target"
    else:
        last_line = next((line.strip() for line in reversed(clean.splitlines()) if line.strip()), "")
        if last_line:
            reason = last_line[:28]

    reachable = received > 0
    return ProbeResult(
        target=target,
        sent=sent,
        received=received,
        loss_pct=loss,
        rtt_ms=rtt_ms,
        hops=hops,
        rssi_dbm=rssi_dbm,
        snr_db=snr_db,
        link_quality_pct=link_quality_pct,
        reachable=reachable,
        reason=reason,
        exit_code=exit_code,
    )


def parse_target_line(line: str, default_full_name: str) -> Target:
    if "|" in line:
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 3:
            label, full_name, destination_hash = parts
        elif len(parts) == 2:
            label, destination_hash = parts
            full_name = default_full_name
        else:
            raise ValueError("Invalid pipe format (expected label|hash or label|full_name|hash)")
    else:
        parts = line.split()
        if len(parts) == 1:
            destination_hash = parts[0]
            full_name = default_full_name
            label = destination_hash[:8]
        elif len(parts) == 2:
            full_name, destination_hash = parts
            label = destination_hash[:8]
        elif len(parts) >= 3:
            label = parts[0]
            full_name = parts[1]
            destination_hash = parts[2]
        else:
            raise ValueError("Could not parse target line")

    if not HASH_RE.match(destination_hash):
        raise ValueError(f"Invalid destination hash: {destination_hash}")

    return Target(label=label, full_name=full_name, destination_hash=destination_hash.lower())


def load_targets(targets_file: Path, default_full_name: str) -> list[Target]:
    if not targets_file.exists():
        raise RuntimeError(f"Targets file not found: {targets_file}")

    targets: list[Target] = []
    for raw in targets_file.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        targets.append(parse_target_line(stripped, default_full_name))

    if not targets:
        raise RuntimeError(f"No targets found in {targets_file}")
    return targets


def resolve_targets_file(targets_file_arg: str) -> Path:
    requested = Path(targets_file_arg).expanduser()
    if targets_file_arg in (DEFAULT_TARGETS_FILE, f"./{DEFAULT_TARGETS_FILE}"):
        if LOCAL_TARGETS_FILE.exists():
            return LOCAL_TARGETS_FILE
    return requested


def format_rtt(rtt_ms: float | None) -> str:
    if rtt_ms is None:
        return "-"
    if rtt_ms >= 1000:
        return f"{(rtt_ms/1000):.2f}s"
    return f"{rtt_ms:.1f}ms"


def format_rf_quality(
    result: ProbeResult,
    *,
    include_link_quality: bool = True,
) -> str:
    parts: list[str] = []
    if result.rssi_dbm is not None:
        parts.append(f"rssi={result.rssi_dbm:.0f}dBm")
    if result.snr_db is not None:
        parts.append(f"snr={result.snr_db:.1f}dB")
    if include_link_quality and result.link_quality_pct is not None:
        parts.append(f"lq={result.link_quality_pct:.0f}%")
    return " ".join(parts)


def format_rf_quality_compact(
    result: ProbeResult,
    *,
    include_link_quality: bool = False,
) -> str:
    parts: list[str] = []
    if result.rssi_dbm is not None:
        parts.append(f"R{result.rssi_dbm:.0f}")
    if result.snr_db is not None:
        parts.append(f"S{result.snr_db:.1f}")
    if include_link_quality and result.link_quality_pct is not None:
        parts.append(f"L{result.link_quality_pct:.0f}")
    return " ".join(parts)


def trim(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def summarize(port: str, elapsed_s: float, results: Iterable[ProbeResult]) -> None:
    result_list = list(results)
    total = len(result_list)
    reachable = sum(1 for r in result_list if r.reachable)
    unreachable = total - reachable

    print(f"RNS traveller check | port={port}")
    print(
        f"targets={total} reachable={reachable} unreachable={unreachable} elapsed={elapsed_s:.1f}s"
    )
    print("-" * 78)
    print("st  label                 recv/loss      rtt    hops  rf                   reason")
    print("-" * 78)
    for result in result_list:
        status = "OK" if result.reachable else "NO"
        recv_loss = f"{result.received}/{result.sent} {result.loss_pct:.0f}%"
        hops_text = "-" if result.hops is None else str(result.hops)
        rf_text = format_rf_quality(result)
        print(
            f"{status:<3}"
            f"{trim(result.target.label, 20):<21}"
            f"{recv_loss:<14}"
            f"{format_rtt(result.rtt_ms):>9}  "
            f"{hops_text:>4}  "
            f"{trim(rf_text or '-', 20):<21}"
            f"{trim(result.reason, 20)}"
        )


def start_rnsd(config_dir: Path, log_path: Path) -> subprocess.Popen[str]:
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(  # noqa: S603
        ["rnsd", "--config", str(config_dir)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def run_probes(
    config_dir: Path,
    targets: list[Target],
    probes: int,
    timeout: float,
    wait: float,
    heartbeat_seconds: float,
    show_progress: bool,
    verbose: bool,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    total = len(targets)
    for index, target in enumerate(targets, start=1):
        cmd = [
            "rnprobe",
            "--config",
            str(config_dir),
            "-n",
            str(probes),
            "-t",
            str(timeout),
            "-w",
            str(wait),
            target.full_name,
            target.destination_hash,
        ]

        if show_progress:
            status(f"[{index}/{total}] probing {target.label}")

        run = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        heartbeat = max(heartbeat_seconds, 0.0)
        next_heartbeat = time.monotonic() + heartbeat if heartbeat > 0 else None
        while run.poll() is None:
            if show_progress and next_heartbeat is not None and time.monotonic() >= next_heartbeat:
                print(".", end="", flush=True)
                next_heartbeat += heartbeat
            time.sleep(0.2)

        stdout, stderr = run.communicate()
        output = (stdout or "") + (stderr or "")
        if verbose:
            print(f"\n--- {target.label} raw rnprobe output ---")
            print(output.strip() or "(no output)")

        parsed = parse_probe_result(target, output, run.returncode)

        results.append(parsed)
        if show_progress:
            probe_status = "OK" if parsed.reachable else "NO"
            print(f"  -> {probe_status}", flush=True)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Auto-detect RNode serial, run rnsd, and summarize rnprobe reachability."
    )
    parser.add_argument(
        "--base-config-dir",
        default="~/.reticulum",
        help="Base Reticulum config directory to clone (default: ~/.reticulum).",
    )
    parser.add_argument(
        "--targets-file",
        default=DEFAULT_TARGETS_FILE,
        help=(
            "Path to targets file "
            "(default: config/targets.txt; auto-uses config/targets.local.txt if present)."
        ),
    )
    parser.add_argument(
        "--default-full-name",
        default="rnstransport.probe",
        help="Default full destination name when omitted in targets file.",
    )
    parser.add_argument("--port", default=None, help="Override serial port auto-detection.")
    parser.add_argument("--probes", type=int, default=1, help="Probes per target.")
    parser.add_argument("--timeout", type=float, default=12.0, help="Probe timeout seconds.")
    parser.add_argument("--wait", type=float, default=0.0, help="Wait between probes seconds.")
    parser.add_argument("--gpsd", action="store_true", help="Try GPS fix from local gpsd (127.0.0.1:2947).")
    parser.add_argument("--gpsd-timeout", type=float, default=6.0, help="Seconds to wait for gpsd fix.")
    parser.add_argument("--lat", type=float, default=None, help="Manual latitude override.")
    parser.add_argument("--lon", type=float, default=None, help="Manual longitude override.")
    parser.add_argument("--alt", type=float, default=None, help="Manual altitude (meters) override.")
    parser.add_argument("--ups-hat-c", action="store_true", help="Read battery from Waveshare UPS HAT (C) INA219.")
    parser.add_argument("--ups-i2c-bus", type=int, default=1, help="I2C bus index for UPS HAT (C) (default: 1).")
    parser.add_argument(
        "--ups-i2c-addr",
        default="0x43",
        help="I2C address for UPS HAT (C) INA219 (default: 0x43).",
    )
    parser.add_argument(
        "--history-file",
        default="logs/traveller-history.csv",
        help="Append per-target run history CSV to this path.",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Disable CSV history logging.",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=5.0,
        help="Print heartbeat dots while probes run (set 0 to disable).",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable per-target progress output.",
    )
    parser.add_argument(
        "--startup-seconds",
        type=float,
        default=4.0,
        help="Minimum seconds to wait before checking readiness.",
    )
    parser.add_argument(
        "--ready-timeout-seconds",
        type=float,
        default=20.0,
        help="Maximum seconds to wait for serial interfaces to report online via rnstatus.",
    )
    parser.add_argument(
        "--ready-poll-seconds",
        type=float,
        default=0.5,
        help="Polling interval for readiness checks.",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List serial candidates and exit.",
    )
    parser.add_argument(
        "--keep-runtime",
        action="store_true",
        help="Keep temporary runtime directory for inspection.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra diagnostics including raw probe outputs.",
    )

    args = parser.parse_args()
    status("traveller_probe: starting")

    if args.list_ports:
        candidates = list_serial_candidates()
        chosen_port = args.port if args.port else (sorted(candidates, key=serial_score, reverse=True)[0] if candidates else None)
        print("Serial candidates:")
        if not candidates:
            print("  (none found)")
            return 0
        for candidate in candidates:
            marker = "*" if candidate == chosen_port else " "
            print(f" {marker} {candidate}")
        return 0

    for required_cmd in ("rnsd", "rnprobe", "rnstatus"):
        if not command_exists(required_cmd):
            eprint(f"{required_cmd} not found in PATH.")
            return 127

    requested_targets_path = Path(args.targets_file).expanduser()
    targets_path = resolve_targets_file(args.targets_file)
    if targets_path != requested_targets_path:
        status(f"traveller_probe: using local targets file {targets_path}")
    targets = load_targets(targets_path, args.default_full_name)
    status(f"traveller_probe: loaded {len(targets)} targets from {targets_path}")

    runtime_dir = Path(tempfile.mkdtemp(prefix="pi-rns-traveller-"))
    rnsd_proc: subprocess.Popen[str] | None = None
    started = time.monotonic()
    run_started_utc = dt.datetime.now(dt.timezone.utc)
    battery = BatteryStatus(
        voltage_v=None,
        current_a=None,
        power_w=None,
        percent=None,
        source="none",
    )
    exit_code = 0

    def cleanup(*_: object) -> None:
        nonlocal rnsd_proc
        if rnsd_proc and rnsd_proc.poll() is None:
            rnsd_proc.terminate()
            try:
                rnsd_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rnsd_proc.kill()
        if args.keep_runtime:
            print(f"Runtime config kept: {runtime_dir}")
        else:
            shutil.rmtree(runtime_dir, ignore_errors=True)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        base_config_dir = Path(args.base_config_dir).expanduser()
        status(f"traveller_probe: preparing config from {base_config_dir}")
        runtime_cfg = ensure_runtime_config(base_config_dir, runtime_dir)
        configured_port = extract_configured_serial_port(runtime_cfg)

        if args.port:
            chosen_port = args.port
            status(f"traveller_probe: using --port override {chosen_port}")
        elif configured_port:
            chosen_port = configured_port
            status(f"traveller_probe: using configured port {chosen_port}")
        else:
            chosen_port, _ = detect_serial_port(None)
            status(f"traveller_probe: auto-detected port {chosen_port}")

        if args.ups_hat_c:
            ups_addr = int(str(args.ups_i2c_addr), 0)
            battery = read_ups_hat_c_status(i2c_bus=args.ups_i2c_bus, addr=ups_addr)
            if battery.voltage_v is not None:
                current_text = "-" if battery.current_a is None else f"{battery.current_a:.3f}A"
                power_text = "-" if battery.power_w is None else f"{battery.power_w:.3f}W"
                pct_text = "-" if battery.percent is None else f"{battery.percent:.1f}%"
                status(
                    "traveller_probe: battery "
                    f"{battery.voltage_v:.3f}V {current_text} "
                    f"{power_text} {pct_text} ({battery.source})"
                )
            else:
                status(
                    "traveller_probe: battery unavailable "
                    f"({battery.source}{': ' + battery.error if battery.error else ''})"
                )

        instance_name = f"traveller-{os.getpid()}"
        patched = patch_config(runtime_cfg, chosen_port, instance_name)
        if not patched:
            eprint(
                "Could not patch serial interface in config. "
                "Add a RNode/KISS interface block or use __SERIAL_PORT__ placeholder."
            )
            return 2

        rnsd_log = runtime_dir / "rnsd.log"
        status("traveller_probe: starting rnsd")
        rnsd_proc = start_rnsd(runtime_dir, rnsd_log)
        status(
            "traveller_probe: waiting for rnsd readiness "
            f"(startup={max(args.startup_seconds, 0):.1f}s, "
            f"timeout={max(args.ready_timeout_seconds, 0):.1f}s)"
        )
        ready, ready_detail, waited_s = wait_for_rnsd_ready(
            config_dir=runtime_dir,
            runtime_cfg=runtime_cfg,
            rnsd_proc=rnsd_proc,
            startup_seconds=args.startup_seconds,
            ready_timeout_seconds=args.ready_timeout_seconds,
            ready_poll_seconds=args.ready_poll_seconds,
        )
        if not ready:
            log_tail = rnsd_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]
            eprint(f"rnsd not ready after {waited_s:.1f}s ({ready_detail}). Recent log lines:")
            for line in log_tail:
                eprint(f"  {line}")
            return 3
        status(f"traveller_probe: rnsd ready after {waited_s:.1f}s ({ready_detail})")

        status("traveller_probe: running probes")
        results = run_probes(
            config_dir=runtime_dir,
            targets=targets,
            probes=args.probes,
            timeout=args.timeout,
            wait=args.wait,
            heartbeat_seconds=args.heartbeat_seconds,
            show_progress=not args.no_progress,
            verbose=args.verbose,
        )
        elapsed = time.monotonic() - started
        location = resolve_location(
            manual_lat=args.lat,
            manual_lon=args.lon,
            manual_alt=args.alt,
            use_gpsd=args.gpsd,
            gpsd_timeout=args.gpsd_timeout,
        )
        status(
            "traveller_probe: location "
            + (
                f"{location.lat:.7f},{location.lon:.7f}"
                if location.lat is not None and location.lon is not None
                else "unavailable"
            )
            + f" ({location.source})"
        )
        summarize(chosen_port, elapsed, results)
        if not args.no_history:
            history_path = Path(args.history_file).expanduser()
            rows = append_history_rows(
                history_file=history_path,
                run_started_utc=run_started_utc,
                elapsed_s=elapsed,
                port=chosen_port,
                location=location,
                battery=battery,
                results=results,
            )
            status(f"traveller_probe: appended {rows} history rows to {history_path}")

        unreachable = sum(1 for result in results if not result.reachable)
        exit_code = 0 if unreachable == 0 else 4
        return exit_code
    finally:
        cleanup()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        eprint(f"Error: {exc}")
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        eprint(f"Command failed: {' '.join(exc.cmd)} (exit {exc.returncode})")
        if exc.stdout:
            eprint(exc.stdout.strip())
        if exc.stderr:
            eprint(exc.stderr.strip())
        sys.exit(exc.returncode if exc.returncode else 1)
