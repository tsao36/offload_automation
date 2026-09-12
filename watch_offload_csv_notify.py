from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip() or default


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_tail_line(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""


def _beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


def _show_popup(title: str, message: str) -> None:
    # Lightweight Windows popup (fallback, no extra dependency).
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
    except Exception:
        pass


_SHARED_LOG_FIELDS = [
    "timestamp_utc",
    "table",
    "event_type",
    "subject",
    "case_number",
    "from_owner",
    "to_owner",
    "status",
    "reason",
    "source_count",
    "target_count",
    "trigger_count",
    "reminder_recipient_count",
    "recommendation_recipient_count",
    "shared_file_url",
]


def _to_int(value: object) -> int:
    try:
        return int(float(str(value or "").strip()))
    except Exception:
        return 0


def _parse_shared_csv_last_row(last_line: str) -> dict[str, str]:
    line = (last_line or "").strip()
    if not line:
        return {}

    try:
        reader = csv.DictReader(io.StringIO(line), fieldnames=_SHARED_LOG_FIELDS)
        row = next(reader, None)
        if not row:
            return {}
        return {str(k): str(v or "").strip() for k, v in row.items()}
    except Exception:
        return {}


def _is_real_offload_recommendation(last_line: str) -> tuple[bool, str]:
    row = _parse_shared_csv_last_row(last_line)
    if not row:
        return False, "unable_to_parse_row"

    event_type = row.get("event_type", "").lower()
    status = row.get("status", "").lower()
    recommendation_count = _to_int(row.get("recommendation_recipient_count"))

    if event_type != "run_sent":
        return False, f"event_type={event_type or 'empty'}"
    if status != "sent":
        return False, f"status={status or 'empty'}"
    if recommendation_count <= 0:
        return False, "recommendation_count=0"

    return True, "ok"


@dataclass
class FileSnapshot:
    exists: bool
    size: int
    mtime_ns: int
    last_line: str

    def key(self) -> str:
        return f"{int(self.exists)}|{self.size}|{self.mtime_ns}|{self.last_line}"


class WatchState:
    def __init__(self, state_file: Path) -> None:
        self._state_file = state_file
        self.last_key: str = ""

    def load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            self.last_key = str(data.get("last_key") or "")
        except Exception:
            self.last_key = ""

    def save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_key": self.last_key,
            "updated_at": _utc_now(),
        }
        self._state_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _snapshot(path: Path) -> FileSnapshot:
    if not path.exists() or not path.is_file():
        return FileSnapshot(False, 0, 0, "")

    stat = path.stat()
    return FileSnapshot(
        True,
        int(stat.st_size),
        int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
        _safe_tail_line(path),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch offload CSV and show local notifications on changes.")
    parser.add_argument(
        "--csv-path",
        default=_env_str("OFFLOAD_SHARED_CSV_PATH", ""),
        help="Path to the shared CSV file to monitor. Defaults to OFFLOAD_SHARED_CSV_PATH.",
    )
    parser.add_argument(
        "--state-file",
        default=_env_str("OFFLOAD_WATCH_STATE_FILE", ".offload_csv_watch_state.json"),
        help="Path to watcher state JSON file.",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=_env_int("OFFLOAD_WATCH_INTERVAL_SEC", 60),
        help="Polling interval in seconds.",
    )
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument("--no-popup", action="store_true", help="Disable popup notification.")
    parser.add_argument("--no-beep", action="store_true", help="Disable beep notification.")
    return parser.parse_args()


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    args = parse_args()

    csv_path = Path(str(args.csv_path or "").strip())
    if not str(csv_path):
        print("[watcher] OFFLOAD_SHARED_CSV_PATH is empty. Set it in .env or pass --csv-path.")
        return 2

    state = WatchState(Path(str(args.state_file or ".offload_csv_watch_state.json")).resolve())
    state.load()

    print(f"[watcher] monitoring: {csv_path}")
    print(f"[watcher] state file: {state._state_file}")
    print(f"[watcher] interval: {int(args.interval_sec)} sec")

    first_pass = True
    while True:
        snap = _snapshot(csv_path)
        key = snap.key()

        if first_pass and not state.last_key:
            state.last_key = key
            state.save()
            print(f"[{_utc_now()}] baseline captured.")
        elif key != state.last_key:
            state.last_key = key
            state.save()
            print(f"[{_utc_now()}] CHANGE DETECTED")
            # Alert only when recommendation email itself was sent.
            is_recommendation, reason = _is_real_offload_recommendation(snap.last_line)
            if is_recommendation:
                msg = (
                    f"New offload recommendation sent!\n"
                    f"UTC: {_utc_now()}\n"
                    f"Last row: {snap.last_line[:260]}"
                )
                print(msg)
                if not args.no_beep:
                    _beep()
                if not args.no_popup:
                    _show_popup("Offload Action", msg)
            else:
                print(f"[skip popup] {reason}: {snap.last_line[:120]}")

        first_pass = False
        if args.once:
            return 0
        time.sleep(max(5, int(args.interval_sec)))


if __name__ == "__main__":
    sys.exit(main())
