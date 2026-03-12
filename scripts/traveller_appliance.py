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
from typing import Any, Callable

from traveller_probe import (
    DEFAULT_TARGETS_FILE,
    BatteryStatus,
    ProbeResult,
    Target,
    detect_serial_port,
    ensure_runtime_config,
    extract_configured_serial_port,
    format_rf_quality,
    format_rf_quality_compact,
    format_rtt,
    load_targets,
    parse_probe_result,
    patch_config,
    read_ups_hat_c_status,
    resolve_targets_file,
    resolve_location,
    start_rnsd,
    wait_for_rnsd_ready,
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


def consume_trigger_file(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except IsADirectoryError:
        print(f"trigger path is a directory, expected file: {path}", flush=True)
        return False
    except OSError as exc:
        print(f"could not consume trigger file {path}: {exc}", flush=True)
        return False


def sleep_with_trigger(
    duration_seconds: int,
    trigger_file: Path,
    stop_requested: Callable[[], bool],
    tick: Callable[[int], None] | None = None,
) -> bool:
    deadline = time.monotonic() + max(int(duration_seconds), 0)
    last_remaining: int | None = None

    while not stop_requested():
        if consume_trigger_file(trigger_file):
            return True

        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            return False

        if tick is not None and remaining != last_remaining:
            tick(remaining)
            last_remaining = remaining

        time.sleep(1)

    return False


def trim_ascii(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def compact_reason(reason: str) -> str:
    normalized = reason.strip().lower()
    if not normalized:
        return "FAIL"
    if "path-timeout" in normalized or "timeout" in normalized:
        return "TIMEOUT"
    if "refused" in normalized:
        return "REFUSED"
    if "no path" in normalized or "no-path" in normalized:
        return "NO PATH"
    if "busy" in normalized:
        return "BUSY"
    if "radio" in normalized:
        return "RADIO"
    if "error" in normalized:
        return "ERROR"
    return "FAIL"


def compact_probe_detail(result: ProbeResult) -> str:
    if result.reachable:
        rf_text = format_rf_quality_compact(result, include_link_quality=True)
        if rf_text:
            return rf_text
        if result.rtt_ms is not None:
            if result.rtt_ms >= 1000:
                return f"{(result.rtt_ms / 1000):.1f}s"
            return f"{result.rtt_ms:.0f}ms"
    return compact_reason(result.reason)


def build_cards(
    targets: list[Target],
    results: list[ProbeResult],
    active_zero_index: int | None = None,
) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for index, target in enumerate(targets):
        if index < len(results):
            result = results[index]
            state = "ok" if result.reachable else "fail"
            detail = compact_probe_detail(result)
        elif active_zero_index is not None and index == active_zero_index:
            state = "active"
            detail = "PROBE"
        else:
            state = "pending"
            detail = "--"
        cards.append(
            {
                "label": trim_ascii(target.label, 13),
                "state": state,
                "detail": detail,
            }
        )
    return cards


def print_console_results(
    run_id: int,
    chosen_port: str,
    targets_path: Path,
    started: dt.datetime,
    results: list[ProbeResult],
) -> None:
    elapsed_s = (now_utc() - started).total_seconds()
    total = len(results)
    reachable = sum(1 for result in results if result.reachable)
    unreachable = total - reachable

    print(
        f"RNS traveller appliance run#{run_id} | port={chosen_port} | targets-file={targets_path}",
        flush=True,
    )
    print(
        f"targets={total} reachable={reachable} unreachable={unreachable} elapsed={elapsed_s:.1f}s",
        flush=True,
    )
    print("-" * 78, flush=True)
    print("st  label                 recv/loss      rtt    hops  rf                   reason", flush=True)
    print("-" * 78, flush=True)
    for result in results:
        st = "OK" if result.reachable else "NO"
        recv_loss = f"{result.received}/{result.sent} {result.loss_pct:.0f}%"
        hops_text = "-" if result.hops is None else str(result.hops)
        rf_text = format_rf_quality(result)
        print(
            f"{st:<3}"
            f"{trim_ascii(result.target.label, 20):<21}"
            f"{recv_loss:<14}"
            f"{format_rtt(result.rtt_ms):>9}  "
            f"{hops_text:>4}  "
            f"{trim_ascii(rf_text or '-', 20):<21}"
            f"{trim_ascii(result.reason, 20)}",
            flush=True,
        )


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

    def __init__(self, requested_driver: str, rotate: int, partial_every: int) -> None:
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
        self.partial_every = max(int(partial_every), 1)
        self.partial_updates = 0
        self.refresh_mode = "full"
        self.partial_ready = False

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

    def _text_size(self, draw: Any, text: str) -> tuple[int, int]:
        if hasattr(draw, "textbbox"):
            x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=self.font)
            return (x1 - x0, y1 - y0)
        return draw.textsize(text, font=self.font)

    def _fit_text(self, draw: Any, text: str, max_width: int) -> str:
        fitted = text
        while fitted and self._text_size(draw, fitted)[0] > max_width:
            fitted = fitted[:-1]
        if fitted == text:
            return fitted
        if len(fitted) >= 3:
            fitted = fitted[:-3] + "..."
        return fitted

    def _grid_dims(self, count: int) -> tuple[int, int]:
        if count <= 1:
            return (1, 1)
        if count == 2:
            return (2, 1)
        if count <= 4:
            return (2, 2)
        if count <= 6:
            return (3, 2)
        return (3, 3)

    def _draw_cards(self, draw: Any, cards: list[dict[str, str]], top: int, empty_text: str) -> None:
        if not cards:
            draw.rectangle((4, top + 2, self.width - 5, self.height - 5), outline=0)
            draw.text((8, top + 8), self._fit_text(draw, empty_text, self.width - 16), font=self.font, fill=0)
            return

        cols, rows = self._grid_dims(len(cards))
        gap = 3
        left = 3
        right = self.width - 4
        bottom = self.height - 4
        area_w = max(right - left, 1)
        area_h = max(bottom - top, 1)
        cell_w = max((area_w - gap * (cols - 1)) // cols, 30)
        cell_h = max((area_h - gap * (rows - 1)) // rows, 20)

        max_cells = cols * rows
        for idx, card in enumerate(cards[:max_cells]):
            row = idx // cols
            col = idx % cols
            x0 = left + col * (cell_w + gap)
            y0 = top + row * (cell_h + gap)
            x1 = x0 + cell_w
            y1 = y0 + cell_h

            state = str(card.get("state", "pending"))
            fill = None
            if state == "ok":
                fill = 0
            draw.rectangle((x0, y0, x1, y1), outline=0, fill=fill)
            if state == "active":
                draw.rectangle((x0 + 1, y0 + 1, x1 - 1, y1 - 1), outline=0)

            text_fill = 255 if fill == 0 else 0
            label = self._fit_text(draw, str(card.get("label", "-")), max(cell_w - 6, 10))
            detail = self._fit_text(draw, str(card.get("detail", "--")), max(cell_w - 6, 10))

            draw.text((x0 + 3, y0 + 2), label, font=self.font, fill=text_fill)
            if cell_h >= 22:
                draw.text((x0 + 3, y0 + 12), detail, font=self.font, fill=text_fill)

            if state == "fail":
                draw.line((x1 - 10, y0 + 3, x1 - 3, y0 + 10), fill=0)
                draw.line((x1 - 3, y0 + 3, x1 - 10, y0 + 10), fill=0)

    def _render_image(self, snapshot: dict[str, Any]) -> Any:
        image = self.Image.new("1", (self.width, self.height), 255)
        draw = self.ImageDraw.Draw(image)

        stage = str(snapshot.get("stage", "?"))
        header_h = 18
        draw.rectangle((0, 0, self.width - 1, self.height - 1), outline=0)
        draw.line((1, header_h, self.width - 2, header_h), fill=0)

        draw.text((4, 4), "RNS Traveller", font=self.font, fill=0)

        stage_text = trim_ascii(stage, 10)
        stage_box_w = 72
        sx0 = self.width - stage_box_w - 3
        draw.rectangle((sx0, 2, self.width - 3, header_h - 2), outline=0, fill=0)
        stage_text = self._fit_text(draw, stage_text, stage_box_w - 6)
        draw.text((sx0 + 3, 4), stage_text, font=self.font, fill=255)

        batt = snapshot.get("battery_pct")
        volt = snapshot.get("battery_v")
        if batt is not None and volt is not None:
            batt_text = f"BAT {batt:.0f}% {volt:.2f}V"
        elif batt is not None:
            batt_text = f"BAT {batt:.0f}%"
        else:
            batt_text = "BAT --"

        lat = snapshot.get("lat")
        lon = snapshot.get("lon")
        gps_text = "GPS --"
        if lat is not None and lon is not None:
            gps_text = f"GPS {lat:.3f},{lon:.3f}"

        port_text = "PORT " + short_port(snapshot.get("serial_port"))
        draw.text((4, 22), self._fit_text(draw, batt_text, 118), font=self.font, fill=0)
        draw.text((4, 33), self._fit_text(draw, gps_text, 118), font=self.font, fill=0)
        draw.text((123, 22), self._fit_text(draw, port_text, self.width - 128), font=self.font, fill=0)

        total_targets = int(snapshot.get("total_targets") or 0)
        reachable = int(snapshot.get("reachable") or 0)
        unreachable = int(snapshot.get("unreachable") or 0)
        status_text = f"OK {reachable}/{total_targets} NO {unreachable}"
        draw.text((123, 33), self._fit_text(draw, status_text, self.width - 128), font=self.font, fill=0)

        footer = str(snapshot.get("message") or snapshot.get("summary") or "")
        if footer:
            draw.text((4, 44), self._fit_text(draw, trim_ascii(footer, 45), self.width - 10), font=self.font, fill=0)

        cards = snapshot.get("cards")
        stage_upper = stage.upper()
        empty_text = "No targets configured"
        if stage_upper == "WAIT":
            empty_text = "Waiting for next run"
        elif stage_upper == "BOOTING":
            empty_text = "Starting..."
        elif stage_upper == "ERROR":
            empty_text = "Error state - restart device"
        if isinstance(cards, list):
            norm_cards: list[dict[str, str]] = []
            for item in cards:
                if not isinstance(item, dict):
                    continue
                norm_cards.append(
                    {
                        "label": str(item.get("label", "-")),
                        "state": str(item.get("state", "pending")),
                        "detail": str(item.get("detail", "--")),
                    }
                )
            self._draw_cards(draw, norm_cards, top=56, empty_text=empty_text)
        else:
            self._draw_cards(draw, [], top=56, empty_text=empty_text)

        if self.rotate in (90, 180, 270):
            image = image.rotate(self.rotate, expand=True, fillcolor=255)
        return image

    def _display_full(self, image: Any) -> None:
        if self.refresh_mode != "full":
            self.epd.init()
            self.refresh_mode = "full"
        self.epd.display(self.epd.getbuffer(image))
        self.partial_ready = False
        self.partial_updates = 0

    def _display_partial(self, image: Any) -> bool:
        if not hasattr(self.epd, "displayPartial"):
            return False
        if not hasattr(self.epd, "displayPartBaseImage"):
            return False

        if self.partial_updates >= self.partial_every:
            return False

        if self.refresh_mode != "partial":
            if hasattr(self.epd, "init_fast"):
                self.epd.init_fast()
            else:
                self.epd.init()
            self.refresh_mode = "partial"
            self.partial_ready = False

        buffer = self.epd.getbuffer(image)
        if not self.partial_ready:
            self.epd.displayPartBaseImage(buffer)
            self.partial_ready = True
        else:
            self.epd.displayPartial(buffer)
        self.partial_updates += 1
        return True

    def render(self, snapshot: dict[str, Any], allow_partial: bool) -> None:
        image = self._render_image(snapshot)
        if allow_partial and self._display_partial(image):
            return
        self._display_full(image)

    def close(self) -> None:
        try:
            if hasattr(self.epd, "sleep"):
                self.epd.sleep()
        except Exception:
            pass


class Display:
    PARTIAL_STAGES = {"CHECKING", "WAIT"}

    def __init__(
        self,
        use_epd: bool,
        epd_driver: str,
        rotate: int,
        epd_partial_every: int,
    ) -> None:
        self.console = ConsoleBackend()
        self.epd: EpaperBackend | None = None
        self._last_payload = ""
        self._last_stage = ""

        if use_epd:
            try:
                self.epd = EpaperBackend(epd_driver, rotate, epd_partial_every)
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
            stage = str(snapshot.get("stage", ""))
            allow_partial = (
                not force
                and stage in self.PARTIAL_STAGES
                and stage == self._last_stage
            )
            self.epd.render(snapshot, allow_partial=allow_partial)
            self._last_stage = stage

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
            rssi_dbm REAL,
            snr_db REAL,
            link_quality_pct REAL,
            reason TEXT,
            exit_code INTEGER NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
        );
        """
    )
    existing_probe_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(probe_results);").fetchall()
    }
    for col_name, col_type in (
        ("rssi_dbm", "REAL"),
        ("snr_db", "REAL"),
        ("link_quality_pct", "REAL"),
    ):
        if col_name not in existing_probe_columns:
            conn.execute(f"ALTER TABLE probe_results ADD COLUMN {col_name} {col_type};")
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
            reachable, sent, received, loss_pct, rtt_ms, hops,
            rssi_dbm, snr_db, link_quality_pct, reason, exit_code
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
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
            result.rssi_dbm,
            result.snr_db,
            result.link_quality_pct,
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


def _coerce_process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def compute_probe_hard_timeout(probes: int, timeout: float, wait: float, explicit_timeout: float) -> float:
    if explicit_timeout > 0:
        return explicit_timeout
    per_probe = max(timeout, 0.0) + max(wait, 0.0) + 3.0
    return max(25.0, (max(probes, 1) * per_probe) + 8.0)


def probe_target(
    config_dir: Path,
    target: Target,
    probes: int,
    timeout: float,
    wait: float,
    hard_timeout: float,
) -> ProbeResult:
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
    effective_timeout = compute_probe_hard_timeout(probes, timeout, wait, hard_timeout)
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=effective_timeout)  # noqa: S603
        output = (run.stdout or "") + (run.stderr or "")
        return parse_probe_result(target, output, run.returncode)
    except subprocess.TimeoutExpired as exc:
        output = _coerce_process_text(exc.stdout) + _coerce_process_text(exc.stderr)
        output += f"\nprobe timed out (hard-timeout {effective_timeout:.1f}s)\n"
        return parse_probe_result(target, output, 124)


def render_rows(results: list[ProbeResult]) -> list[str]:
    rows: list[str] = []
    for result in results[:3]:
        st = "OK" if result.reachable else "NO"
        rf_text = format_rf_quality_compact(result, include_link_quality=False)
        if result.reachable and rf_text:
            metric = rf_text
        else:
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
        "cards": [],
        "total_targets": 0,
        "reachable": 0,
        "unreachable": 0,
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
                "cards": [],
                "total_targets": 0,
                "reachable": 0,
                "unreachable": 0,
            }
        )
        display.update(snapshot)
        atomic_write_json(state_file, snapshot)

        if not patch_config(runtime_cfg, chosen_port, f"traveller-appliance-{os.getpid()}"):
            raise RuntimeError("Could not patch serial interface in Reticulum config")

        rnsd_log = runtime_dir / "rnsd.log"
        rnsd_proc = start_rnsd(runtime_dir, rnsd_log)
        ready, ready_detail, waited_s = wait_for_rnsd_ready(
            config_dir=runtime_dir,
            runtime_cfg=runtime_cfg,
            rnsd_proc=rnsd_proc,
            startup_seconds=args.startup_seconds,
            ready_timeout_seconds=args.ready_timeout_seconds,
            ready_poll_seconds=args.ready_poll_seconds,
        )
        if not ready:
            tail = rnsd_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]
            tail_msg = " | ".join(tail) if tail else "rnsd log empty"
            raise RuntimeError(f"rnsd not ready after {waited_s:.1f}s ({ready_detail}); {tail_msg[:120]}")
        print(f"rnsd ready after {waited_s:.1f}s ({ready_detail})", flush=True)

        targets_path = resolve_targets_file(args.targets_file)
        targets = load_targets(targets_path, args.default_full_name)
        run_id = start_run_record(conn, started, chosen_port, location, battery)
        results: list[ProbeResult] = []

        loc_text = "GPS --"
        if location.lat is not None and location.lon is not None:
            loc_text = f"GPS {location.lat:.6f},{location.lon:.6f}"
        bat_text = "BAT --"
        if battery.percent is not None and battery.voltage_v is not None:
            bat_text = f"BAT {battery.percent:.0f}% {battery.voltage_v:.2f}V ({battery.source})"
        elif battery.voltage_v is not None:
            bat_text = f"BAT {battery.voltage_v:.2f}V ({battery.source})"
        print(
            f"run#{run_id} start | port={chosen_port} | targets={len(targets)} | targets-file={targets_path}",
            flush=True,
        )
        print(f"{bat_text} | {loc_text}", flush=True)

        for index, target in enumerate(targets, start=1):
            cards = build_cards(targets, results, active_zero_index=index - 1)
            reachable = sum(1 for item in results if item.reachable)
            unreachable = len(results) - reachable

            snapshot.update(
                {
                    "stage": "CHECKING",
                    "message": f"{index}/{len(targets)} {target.label}",
                    "summary": f"run#{run_id}",
                    "rows": render_rows(results),
                    "cards": cards,
                    "total_targets": len(targets),
                    "reachable": reachable,
                    "unreachable": unreachable,
                }
            )
            display.update(snapshot)
            atomic_write_json(state_file, snapshot)

            print(f"[{index}/{len(targets)}] probing {target.label}", flush=True)
            result = probe_target(
                runtime_dir,
                target,
                args.probes,
                args.timeout,
                args.wait,
                args.probe_hard_timeout,
            )
            results.append(result)
            record_probe_result(conn, run_id, index, result)

            probe_metric = format_rtt(result.rtt_ms) if result.rtt_ms is not None else result.reason
            hops_text = "-" if result.hops is None else str(result.hops)
            recv_loss = f"{result.received}/{result.sent} {result.loss_pct:.0f}%"
            rf_text = format_rf_quality(result) or "-"
            probe_st = "OK" if result.reachable else "NO"
            print(
                f"  -> {probe_st} {target.label} recv/loss={recv_loss} "
                f"rtt={probe_metric} hops={hops_text} rf={rf_text}",
                flush=True,
            )

        reachable = sum(1 for result in results if result.reachable)
        unreachable = len(results) - reachable
        finish_run_record(conn, run_id, len(results), reachable, unreachable, None)

        snapshot.update(
            {
                "stage": "RESULTS",
                "message": f"{reachable}/{len(results)} reachable",
                "summary": f"run#{run_id} complete",
                "rows": render_rows(results),
                "cards": build_cards(targets, results, active_zero_index=None),
                "total_targets": len(results),
                "reachable": reachable,
                "unreachable": unreachable,
            }
        )
        display.update(snapshot, force=True)
        atomic_write_json(state_file, snapshot)
        print_console_results(run_id, chosen_port, targets_path, started, results)
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
                "cards": [],
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
    parser.add_argument(
        "--probe-hard-timeout",
        type=float,
        default=0.0,
        help="Hard cap in seconds for each rnprobe process (0 = auto-derived).",
    )
    parser.add_argument("--startup-seconds", type=float, default=3.0)
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
    parser.add_argument(
        "--results-hold-seconds",
        type=int,
        default=30,
        help=(
            "Keep RESULTS screen visible for this many seconds before WAIT screen "
            "(counts toward check interval)."
        ),
    )
    parser.add_argument(
        "--trigger-file",
        default="/tmp/pi-rns-traveller.run-now",
        help="Touch this file to trigger an immediate run while waiting.",
    )
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
    parser.add_argument(
        "--epd-partial-every",
        type=int,
        default=5,
        help="Force a full ePaper refresh after this many partial updates (default: 5).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir).expanduser()
    db_path = state_dir / args.db_file
    state_path = state_dir / args.state_file
    trigger_file = Path(args.trigger_file).expanduser()

    for required_cmd in ("rnsd", "rnprobe", "rnstatus"):
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

    display = Display(
        use_epd=not args.no_epd,
        epd_driver=args.epd_driver,
        rotate=args.epd_rotate,
        epd_partial_every=args.epd_partial_every,
    )
    conn = ensure_database(db_path)
    print(
        "appliance config: "
        f"interval={max(int(args.check_interval_seconds), 1)}s "
        f"results_hold={max(int(args.results_hold_seconds), 0)}s "
        f"trigger_file={trigger_file}",
        flush=True,
    )

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
            hold_s = min(max(int(args.results_hold_seconds), 0), wait_s)

            if hold_s > 0:
                print(f"holding RESULTS screen for {hold_s}s", flush=True)
                was_triggered = sleep_with_trigger(
                    hold_s,
                    trigger_file,
                    stop_requested=lambda: stop_requested,
                )
                if stop_requested:
                    break
                if was_triggered:
                    print(f"manual trigger received ({trigger_file}), running now", flush=True)
                    snapshot = {
                        "stage": "WAIT",
                        "message": "manual trigger",
                        "summary": "running now",
                    }
                    display.update(snapshot)
                    atomic_write_json(state_path, snapshot)
                    continue

            remaining_wait_s = wait_s - hold_s
            if remaining_wait_s <= 0:
                continue

            snapshot = {
                "stage": "WAIT",
                "message": f"next run in {remaining_wait_s}s",
                "summary": "waiting",
            }
            display.update(snapshot, force=True)
            atomic_write_json(state_path, snapshot)
            last_announced_remaining = remaining_wait_s

            def wait_tick(remaining: int) -> None:
                nonlocal last_announced_remaining
                should_refresh_wait = (
                    remaining != last_announced_remaining
                    and (remaining <= 10 or remaining % 10 == 0)
                )
                if should_refresh_wait:
                    wait_snapshot = {
                        "stage": "WAIT",
                        "message": f"next run in {remaining}s",
                        "summary": "waiting",
                    }
                    display.update(wait_snapshot)
                    atomic_write_json(state_path, wait_snapshot)
                    last_announced_remaining = remaining

            was_triggered = sleep_with_trigger(
                remaining_wait_s,
                trigger_file,
                stop_requested=lambda: stop_requested,
                tick=wait_tick,
            )
            if stop_requested:
                break
            if was_triggered:
                print(f"manual trigger received ({trigger_file}), running now", flush=True)
                snapshot = {
                    "stage": "WAIT",
                    "message": "manual trigger",
                    "summary": "running now",
                }
                display.update(snapshot)
                atomic_write_json(state_path, snapshot)

        return 0
    finally:
        conn.close()
        display.close()


if __name__ == "__main__":
    sys.exit(main())
