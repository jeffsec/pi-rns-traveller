#!/usr/bin/env python3
"""
Summarize Traveller probe outcomes by time bucket.

Reads either:
  - state/history.db (appliance mode), or
  - logs/traveller-history.csv (probe mode)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


PREFERRED_STATUS_ORDER = [
    "OK",
    "TIMEOUT",
    "NO_PATH",
    "SERIAL",
    "PERMISSION",
    "BAD_TARGET",
    "FAIL",
]


def parse_iso_datetime(value: str) -> dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def bucket_start(ts_utc: dt.datetime, bucket: str) -> dt.datetime:
    if bucket == "minute":
        return ts_utc.replace(second=0, microsecond=0)
    if bucket == "hour":
        return ts_utc.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        return ts_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported bucket: {bucket}")


def classify_status(reachable: int | bool, reason: str) -> str:
    if bool(reachable):
        return "OK"

    normalized = (reason or "").strip().lower()
    if "no path" in normalized or "no-path" in normalized:
        return "NO_PATH"
    if "path-timeout" in normalized or "probe-timeout" in normalized or "timeout" in normalized:
        return "TIMEOUT"
    if "serial" in normalized:
        return "SERIAL"
    if "permission" in normalized or "operation not permitted" in normalized:
        return "PERMISSION"
    if "bad-target" in normalized or "invalid destination" in normalized:
        return "BAD_TARGET"
    return "FAIL"


def iter_rows_from_sqlite(db_path: Path) -> Iterable[tuple[dt.datetime, int, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        query = """
            SELECT probe_results.ts_utc, probe_results.reachable, probe_results.reason
            FROM probe_results
            ORDER BY probe_results.ts_utc ASC;
        """
        for ts_text, reachable, reason in conn.execute(query):
            if ts_text is None:
                continue
            try:
                ts_utc = parse_iso_datetime(str(ts_text))
            except ValueError:
                continue
            yield ts_utc, int(reachable), str(reason or "")
    finally:
        conn.close()


def iter_rows_from_csv(csv_path: Path) -> Iterable[tuple[dt.datetime, int, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts_text = (row.get("run_started_utc") or "").strip()
            if not ts_text:
                continue
            try:
                ts_utc = parse_iso_datetime(ts_text)
            except ValueError:
                continue
            reachable_text = (row.get("reachable") or "0").strip()
            try:
                reachable = int(reachable_text)
            except ValueError:
                reachable = 0
            reason = (row.get("reason") or "").strip()
            yield ts_utc, reachable, reason


def choose_input_source(args: argparse.Namespace) -> tuple[str, Path]:
    if args.source == "sqlite":
        return "sqlite", Path(args.db_file).expanduser()
    if args.source == "csv":
        return "csv", Path(args.csv_file).expanduser()

    db_path = Path(args.db_file).expanduser()
    if db_path.exists():
        return "sqlite", db_path
    csv_path = Path(args.csv_file).expanduser()
    if csv_path.exists():
        return "csv", csv_path
    raise FileNotFoundError(
        f"No input found. Checked db={db_path} and csv={csv_path}. "
        "Use --source sqlite|csv with explicit path."
    )


def summarize(
    rows: Iterable[tuple[dt.datetime, int, str]],
    bucket: str,
    since: dt.datetime | None,
    until: dt.datetime | None,
) -> dict[dt.datetime, Counter[str]]:
    grouped: dict[dt.datetime, Counter[str]] = defaultdict(Counter)
    for ts_utc, reachable, reason in rows:
        if since is not None and ts_utc < since:
            continue
        if until is not None and ts_utc >= until:
            continue
        bucket_ts = bucket_start(ts_utc, bucket)
        status = classify_status(reachable, reason)
        grouped[bucket_ts][status] += 1
    return grouped


def print_table(grouped: dict[dt.datetime, Counter[str]]) -> None:
    if not grouped:
        print("No probe rows matched the selected filters.")
        return

    ordered_keys = sorted(grouped.keys())
    active_statuses = [
        status
        for status in PREFERRED_STATUS_ORDER
        if any(grouped[key][status] > 0 for key in ordered_keys)
    ]
    if not active_statuses:
        active_statuses = ["OK", "FAIL"]

    headers = ["bucket_utc", "total"] + active_statuses
    rows: list[list[str]] = []
    widths = [len(h) for h in headers]

    for key in ordered_keys:
        counter = grouped[key]
        total = sum(counter.values())
        row_values = [key.isoformat(), str(total)] + [str(counter[s]) for s in active_statuses]
        rows.append(row_values)
        for idx, value in enumerate(row_values):
            if len(value) > widths[idx]:
                widths[idx] = len(value)

    def format_row(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    print(format_row(headers))
    print("  ".join("-" * width for width in widths))
    for values in rows:
        print(format_row(values))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize Traveller probe statuses by time.")
    parser.add_argument(
        "--source",
        choices=["auto", "sqlite", "csv"],
        default="auto",
        help="Input source type (default: auto).",
    )
    parser.add_argument(
        "--db-file",
        default="state/history.db",
        help="SQLite history database path (used by appliance mode).",
    )
    parser.add_argument(
        "--csv-file",
        default="logs/traveller-history.csv",
        help="CSV history path (used by traveller_probe.py history mode).",
    )
    parser.add_argument(
        "--bucket",
        choices=["minute", "hour", "day"],
        default="hour",
        help="Time bucket size for counts (default: hour).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Inclusive start time (ISO-8601, interpreted as UTC if no timezone).",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Exclusive end time (ISO-8601, interpreted as UTC if no timezone).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    since = parse_iso_datetime(args.since) if args.since else None
    until = parse_iso_datetime(args.until) if args.until else None
    if since is not None and until is not None and since >= until:
        print("--since must be earlier than --until", file=sys.stderr)
        return 2

    try:
        source_type, source_path = choose_input_source(args)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if source_type == "sqlite":
        rows = iter_rows_from_sqlite(source_path)
    else:
        rows = iter_rows_from_csv(source_path)

    grouped = summarize(rows, bucket=args.bucket, since=since, until=until)
    print(f"Source: {source_type} ({source_path})")
    if since is not None:
        print(f"Since:  {since.isoformat()}")
    if until is not None:
        print(f"Until:  {until.isoformat()}")
    print_table(grouped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
