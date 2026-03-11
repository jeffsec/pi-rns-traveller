#!/usr/bin/env python3
"""
Turn-key appliance runner for Pi RNS Traveller.

Lifecycle:
1) Boot state shown on display.
2) Check state while probing targets.
3) Results state when finished.
4) Error state on failure (holds screen until restart by default).
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from traveller_probe import (
    DEFAULT_TARGETS_FILE,
    BatteryStatus,
    ProbeResult,
    Target,
    detect_serial_port,
    ensure_runtime_config,
    extract_configured_serial_port,
    format_rtt,
    load_targets,
    parse_probe_result,
    patch_config,
    read_ups_hat_c_status,
    resolve_targets_file,
    resolve_location,
    start_rnsd,
)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ts_local() -> str:
    return dt.datetime.now().strftime("%H:%M")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(tmp_path, path)

    try:
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def short_port(port: str | None) -> str:
    if not port:
        return "-"
    if "/dev/serial/by-id/" in port:
        return port.split("/dev/serial/by-id/", 1)[1][:24]
    return Path(port).name[:24]


class ConsoleBackend:
    def __init__(self) -> None:
        self._last = ""

    def render(self, snapshot: dict[str, Any]) -> None:
        stage = snapshot.get("stage", "?")
        msg = snapshot.get("message", "")
        summary = snapshot.get("summary", "")
        key = f"{stage}|{msg}|{summary}"
        if key != self._last:
            print(f"[{ts_local()}] {stage} {msg} {summary}".strip(), flush=True)
            self._last = key

    def close(self) -> None:
        return


class EpaperBackend:
    DRIVER_CANDIDATES = ("epd2in13_V4", "epd2in13_V3", "epd2in13_V2", "epd2in13")

    def __init__(self, requested_driver: str, rotate: int) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"Pillow not available: {exc}") from exc

        self.Image = Image
        self.ImageDraw = ImageDraw
        self.ImageFont = ImageFont
        self.rotate = rotate
        self.driver_name, self.epd = self._init_epd(requested_driver)

        # Waveshare 2.13 examples use height,width for horizontal layout.
        self.width = int(getattr(self.epd, "height", 250))
        self.height = int(getattr(self.epd, "width", 122))
        self.font = self.ImageFont.load_default()

        if hasattr(self.epd, "Clear"):
            self.epd.Clear(0xFF)

    def _init_epd(self, requested_driver: str) -> tuple[str, Any]:
        names = (
            [requested_driver]
            if requested_driver != "auto"
            else list(self.DRIVER_CANDIDATES)
        )

        last_exc: Exception | None = None
        for name in names:
            try:
                module = importlib.import_module(f"waveshare_epd.{name}")
                epd = module.EPD()
                epd.init()
                return name, epd
            except Exception as exc:
                last_exc = exc

        raise RuntimeError(f"Unable to initialize Waveshare 2.13 ePaper driver: {last_exc}")

    def _build_lines(self, snapshot: dict[str, Any]) -> list[str]:
        stage = str(snapshot.get("stage", "?"))
        line1 = f"{stage:<8}{ts_local()}"

        batt = snapshot.get("battery_pct")
        volt = snapshot.get("battery_v")
        if batt is not None:
            batt_str = f"BAT {batt:.0f}% {volt:.2f}V" if volt is not None else f"BAT {batt:.0f}%"
        else:
            batt_str = "BAT --"

        lat = snapshot.get("lat")
        lon = snapshot.get("lon")
        gps = "GPS --"
        if lat is not None and lon is not None:
            gps = f"GPS {lat:.3f},{lon:.3f}"

        lines = [
            line1,
            batt_str,
            gps,
            f"PORT {short_port(snapshot.get('serial_port'))}",
        ]

        message = snapshot.get("message")
        if message:
            lines.append(str(message)[:34])

        summary = snapshot.get("summary")
        if summary:
            lines.append(str(summary)[:34])

        for row in snapshot.get("rows", [])[:3]:
            lines.append(str(row)[:34])

        return lines[:8]

    def render(self, snapshot: dict[str, Any]) -> None:
        image = self.Image.new("1", (self.width, self.height), 255)
        draw = self.ImageDraw.Draw(image)
        y = 2
        for line in self._build_lines(snapshot):
            draw.text((2, y), line, font=self.font, fill=0)
            y += 14

        if self.rotate in (90, 180, 270):
            image = image.rotate(self.rotate, expand=True, fillcolor=255)

        self.epd.display(self.epd.getbuffer(image))

    def close(self) -> None:
        try:
            if hasattr(self.epd, "sleep"):
                self.epd.sleep()
        except Exception:
            pass


class Display:
    def __init__(self, use_epd: bool, epd_driver: str, rotate: int) -> None:
        self.console = ConsoleBackend()
        self.epd: EpaperBackend | None = None
        self._last_payload = ""

        if use_epd:
            try:
                self.epd = EpaperBackend(epd_driver, rotate)
                self.console.render({"stage": "DISPLAY", "message": f"ePaper {self.epd.driver_name}"})
            except Exception as exc:
                self.console.render({"stage": "DISPLAY", "message": f"fallback console ({exc})"})

    def update(self, snapshot: dict[str, Any], force: bool = False) -> None:
        payload = json.dumps(snapshot, sort_keys=True, default=str)
        if not force and payload == self._last_payload:
            return

        self._last_payload = payload
        self.console.render(snapshot)
        if self.epd is not None:
            self.epd.render(snapshot)

    def close(self) -> None:
        self.console.close()
        if self.epd is not None:
            self.epd.close()


def ensure_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_utc TEXT NOT NULL,
            finished_utc TEXT,
            serial_port TEXT,
            gps_source TEXT,
            lat REAL,
            lon REAL,
            alt_m REAL,
            battery_source TEXT,
            battery_voltage_v REAL,
            battery_current_a REAL,
            battery_power_w REAL,
            battery_pct REAL,
            total_targets INTEGER DEFAULT 0,
            reachable_targets INTEGER DEFAULT 0,
            unreachable_targets INTEGER DEFAULT 0,
            error TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS probe_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ts_utc TEXT NOT NULL,
            target_index INTEGER NOT NULL,
            label TEXT NOT NULL,
            full_name TEXT NOT NULL,
            destination_hash TEXT NOT NULL,
            reachable INTEGER NOT NULL,
            sent INTEGER NOT NULL,
            received INTEGER NOT NULL,
            loss_pct REAL NOT NULL,
            rtt_ms REAL,
            hops INTEGER,
            reason TEXT,
            exit_code INTEGER NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_results_run ON probe_results(run_id);")
    conn.commit()
    return conn


def start_run_record(
    conn: sqlite3.Connection,
    started_utc: dt.datetime,
    serial_port: str,
    location: Any,
    battery: Any,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO runs (
            started_utc, serial_port, gps_source, lat, lon, alt_m,
            battery_source, battery_voltage_v, battery_current_a, battery_power_w, battery_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            started_utc.replace(microsecond=0).isoformat(),
            serial_port,
            location.source,
            location.lat,
            location.lon,
            location.alt_m,
            battery.source,
            battery.voltage_v,
            battery.current_a,
            battery.power_w,
            battery.percent,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def record_probe_result(conn: sqlite3.Connection, run_id: int, index: int, result: ProbeResult) -> None:
    conn.execute(
        """
        INSERT INTO probe_results (
            run_id, ts_utc, target_index, label, full_name, destination_hash,
            reachable, sent, received, loss_pct, rtt_ms, hops, reason, exit_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            run_id,
            now_utc().replace(microsecond=0).isoformat(),
            index,
            result.target.label,
            result.target.full_name,
            result.target.destination_hash,
            int(result.reachable),
            result.sent,
            result.received,
            result.loss_pct,
            result.rtt_ms,
            result.hops,
            result.reason,
            result.exit_code,
        ),
    )
    conn.commit()


def finish_run_record(
    conn: sqlite3.Connection,
    run_id: int,
    total: int,
    reachable: int,
    unreachable: int,
    error: str | None,
) -> None:
    conn.execute(
        """
        UPDATE runs
        SET finished_utc = ?, total_targets = ?, reachable_targets = ?, unreachable_targets = ?, error = ?
        WHERE id = ?;
        """,
        (now_utc().replace(microsecond=0).isoformat(), total, reachable, unreachable, error, run_id),
    )
    conn.commit()


def probe_target(config_dir: Path, target: Target, probes: int, timeout: float, wait: float) -> ProbeResult:
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
    return parse_probe_result(target, output, run.returncode)


def render_rows(results: list[ProbeResult]) -> list[str]:
    rows: list[str] = []
    for result in results[:3]:
        st = "OK" if result.reachable else "NO"
        metric = format_rtt(result.rtt_ms) if result.rtt_ms is not None else result.reason
        rows.append(f"{st} {result.target.label} {metric}")
    return rows


def run_cycle(
    args: argparse.Namespace,
    display: Display,
    conn: sqlite3.Connection,
    state_file: Path,
) -> tuple[int, int]:
    started = now_utc()
    runtime_dir = Path(tempfile.mkdtemp(prefix="pi-rns-traveller-appliance-"))
    rnsd_proc: subprocess.Popen[str] | None = None
    run_id: int | None = None

    snapshot: dict[str, Any] = {
        "stage": "BOOTING",
        "message": "Preparing runtime",
        "serial_port": None,
        "rows": [],
    }
    display.update(snapshot, force=True)
    atomic_write_json(state_file, snapshot)

    try:
        runtime_cfg = ensure_runtime_config(Path(args.base_config_dir).expanduser(), runtime_dir)
        configured_port = extract_configured_serial_port(runtime_cfg)

        if args.port:
            chosen_port = args.port
        elif configured_port:
            chosen_port = configured_port
        else:
            chosen_port, _ = detect_serial_port(None)

        location = resolve_location(args.lat, args.lon, args.alt, args.gpsd, args.gpsd_timeout)
        battery = (
            read_ups_hat_c_status(i2c_bus=args.ups_i2c_bus, addr=int(str(args.ups_i2c_addr), 0))
            if args.ups_hat_c
            else BatteryStatus(
                voltage_v=None,
                current_a=None,
                power_w=None,
                percent=None,
                source="disabled",
                error=None,
            )
        )

        snapshot.update(
            {
                "stage": "BOOTING",
                "message": "Configuring radio",
                "serial_port": chosen_port,
                "lat": location.lat,
                "lon": location.lon,
                "battery_pct": battery.percent,
                "battery_v": battery.voltage_v,
            }
        )
        display.update(snapshot)
        atomic_write_json(state_file, snapshot)

        if not patch_config(runtime_cfg, chosen_port, f"traveller-appliance-{os.getpid()}"):
            raise RuntimeError("Could not patch serial interface in Reticulum config")

        rnsd_log = runtime_dir / "rnsd.log"
        rnsd_proc = start_rnsd(runtime_dir, rnsd_log)
        if args.startup_seconds > 0:
            time.sleep(args.startup_seconds)

        if rnsd_proc.poll() is not None:
            tail = rnsd_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-4:]
            message = tail[-1] if tail else "rnsd exited early"
            raise RuntimeError(message)

        targets_path = resolve_targets_file(args.targets_file)
        targets = load_targets(targets_path, args.default_full_name)
        run_id = start_run_record(conn, started, chosen_port, location, battery)
        results: list[ProbeResult] = []

        for index, target in enumerate(targets, start=1):
            snapshot.update(
                {
                    "stage": "CHECKING",
                    "message": f"{index}/{len(targets)} {target.label}",
                    "summary": f"run#{run_id}",
                    "rows": render_rows(results),
                }
            )
            display.update(snapshot)
            atomic_write_json(state_file, snapshot)

            result = probe_target(runtime_dir, target, args.probes, args.timeout, args.wait)
            results.append(result)
            record_probe_result(conn, run_id, index, result)

        reachable = sum(1 for result in results if result.reachable)
        unreachable = len(results) - reachable
        finish_run_record(conn, run_id, len(results), reachable, unreachable, None)

        snapshot.update(
            {
                "stage": "RESULTS",
                "message": f"{reachable}/{len(results)} reachable",
                "summary": f"run#{run_id} complete",
                "rows": render_rows(results),
            }
        )
        display.update(snapshot, force=True)
        atomic_write_json(state_file, snapshot)
        return reachable, unreachable

    except Exception as exc:
        if run_id is not None:
            finish_run_record(conn, run_id, 0, 0, 0, str(exc))

        snapshot.update(
            {
                "stage": "ERROR",
                "message": str(exc)[:48],
                "summary": "Restart device",
                "rows": [],
            }
        )
        display.update(snapshot, force=True)
        atomic_write_json(state_file, snapshot)
        raise
    finally:
        if rnsd_proc and rnsd_proc.poll() is None:
            rnsd_proc.terminate()
            try:
                rnsd_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rnsd_proc.kill()

        if args.keep_runtime:
            print(f"runtime kept: {runtime_dir}", flush=True)
        else:
            shutil.rmtree(runtime_dir, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run pi-rns-traveller as a boot appliance with ePaper status and durable logs."
    )
    parser.add_argument("--base-config-dir", default="~/.reticulum")
    parser.add_argument(
        "--targets-file",
        default=DEFAULT_TARGETS_FILE,
        help="Targets file path (auto-uses config/targets.local.txt when present).",
    )
    parser.add_argument("--default-full-name", default="rnstransport.probe")
    parser.add_argument("--port", default=None, help="Fixed serial port override.")
    parser.add_argument("--probes", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--startup-seconds", type=float, default=3.0)

    parser.add_argument("--gpsd", action="store_true")
    parser.add_argument("--gpsd-timeout", type=float, default=6.0)
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    parser.add_argument("--alt", type=float, default=None)

    parser.add_argument("--ups-hat-c", action="store_true")
    parser.add_argument("--ups-i2c-bus", type=int, default=1)
    parser.add_argument("--ups-i2c-addr", default="0x43")

    parser.add_argument("--state-dir", default="state", help="Persistent path for sqlite and state json.")
    parser.add_argument("--db-file", default="history.db")
    parser.add_argument("--state-file", default="state.json")
    parser.add_argument("--check-interval-seconds", type=int, default=120)
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue periodic checks after an error instead of halting on ERROR screen.",
    )
    parser.add_argument("--keep-runtime", action="store_true")

    parser.add_argument("--no-epd", action="store_true")
    parser.add_argument("--epd-driver", default="auto")
    parser.add_argument("--epd-rotate", type=int, default=0, choices=[0, 90, 180, 270])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir).expanduser()
    db_path = state_dir / args.db_file
    state_path = state_dir / args.state_file

    for required_cmd in ("rnsd", "rnprobe"):
        if shutil.which(required_cmd) is None:
            print(f"missing required command in PATH: {required_cmd}", flush=True)
            return 127

    stop_requested = False

    def on_signal(signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True
        print(f"signal {signum} received, stopping...", flush=True)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    display = Display(use_epd=not args.no_epd, epd_driver=args.epd_driver, rotate=args.epd_rotate)
    conn = ensure_database(db_path)

    try:
        while not stop_requested:
            try:
                run_cycle(args, display, conn, state_path)
            except Exception as exc:
                print(f"cycle failed: {exc}", flush=True)
                if args.once:
                    return 1
                if not args.continue_on_error:
                    # Hold error screen for explicit manual restart.
                    while not stop_requested:
                        time.sleep(1)
                    break

            if args.once or stop_requested:
                break

            wait_s = max(int(args.check_interval_seconds), 1)
            snapshot = {
                "stage": "WAIT",
                "message": f"next run in {wait_s}s",
                "summary": "waiting",
            }
            display.update(snapshot, force=True)
            atomic_write_json(state_path, snapshot)
            time.sleep(wait_s)

        return 0
    finally:
        conn.close()
        display.close()


if __name__ == "__main__":
    sys.exit(main())
