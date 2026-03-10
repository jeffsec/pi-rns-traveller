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
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


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
    reachable: bool
    reason: str
    exit_code: int


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


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


def format_rtt(rtt_ms: float | None) -> str:
    if rtt_ms is None:
        return "-"
    if rtt_ms >= 1000:
        return f"{(rtt_ms/1000):.2f}s"
    return f"{rtt_ms:.1f}ms"


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
    print("st  label                 recv/loss      rtt    hops  reason")
    print("-" * 78)
    for result in result_list:
        status = "OK" if result.reachable else "NO"
        recv_loss = f"{result.received}/{result.sent} {result.loss_pct:.0f}%"
        hops_text = "-" if result.hops is None else str(result.hops)
        print(
            f"{status:<3}"
            f"{trim(result.target.label, 20):<21}"
            f"{recv_loss:<14}"
            f"{format_rtt(result.rtt_ms):>9}  "
            f"{hops_text:>4}  "
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
    verbose: bool,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for target in targets:
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
        run = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
        output = (run.stdout or "") + (run.stderr or "")
        if verbose:
            print(f"\n--- {target.label} raw rnprobe output ---")
            print(output.strip() or "(no output)")
        results.append(parse_probe_result(target, output, run.returncode))
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
        default="config/targets.txt",
        help="Path to targets file (default: config/targets.txt).",
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
    parser.add_argument(
        "--startup-seconds",
        type=float,
        default=4.0,
        help="Seconds to wait for rnsd startup.",
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

    if not command_exists("rnsd"):
        eprint("rnsd not found in PATH.")
        return 127
    if not command_exists("rnprobe"):
        eprint("rnprobe not found in PATH.")
        return 127

    chosen_port, _ = detect_serial_port(args.port)
    targets = load_targets(Path(args.targets_file).expanduser(), args.default_full_name)

    runtime_dir = Path(tempfile.mkdtemp(prefix="pi-rns-traveller-"))
    rnsd_proc: subprocess.Popen[str] | None = None
    started = time.monotonic()
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
        runtime_cfg = ensure_runtime_config(base_config_dir, runtime_dir)
        instance_name = f"traveller-{os.getpid()}"
        patched = patch_config(runtime_cfg, chosen_port, instance_name)
        if not patched:
            eprint(
                "Could not patch serial interface in config. "
                "Add a RNode/KISS interface block or use __SERIAL_PORT__ placeholder."
            )
            return 2

        rnsd_log = runtime_dir / "rnsd.log"
        rnsd_proc = start_rnsd(runtime_dir, rnsd_log)
        time.sleep(max(args.startup_seconds, 0))

        if rnsd_proc.poll() is not None:
            log_tail = rnsd_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]
            eprint("rnsd exited early. Recent log lines:")
            for line in log_tail:
                eprint(f"  {line}")
            return 3

        results = run_probes(
            config_dir=runtime_dir,
            targets=targets,
            probes=args.probes,
            timeout=args.timeout,
            wait=args.wait,
            verbose=args.verbose,
        )
        elapsed = time.monotonic() - started
        summarize(chosen_port, elapsed, results)

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
