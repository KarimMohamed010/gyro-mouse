#!/usr/bin/env python3
"""
Serial dataset recorder for ESP32 + MPU6050 streams.

Expected serial sample format per line:
    ax,ay,az,gx,gy,gz,yaw,pitch,roll

Controls:
    1=swipe, 2=shake, 3=circle, 4=wave, 5=idle
    space=start/stop recording
    q=quit
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import serial
import serial.tools.list_ports

FEATURE_NAMES = ["ax", "ay", "az", "gx", "gy", "gz", "yaw", "pitch", "roll"]
EXPECTED_FEATURE_COUNT = 9
LABEL_KEYS = {
    "1": "swipe",
    "2": "shake",
    "3": "circle",
    "4": "wave",
    "5": "idle",
}


@dataclass
class Sample:
    timestamp: float
    values: list[float]


def parse_sample_line(line: str) -> Optional[list[float]]:
    """Return 9 float features if line is valid, otherwise None."""
    cleaned = line.strip()
    if not cleaned:
        return None

    # Ignore obvious non-sample lines (status text / JSON / garbage).
    if cleaned.startswith("{") or cleaned.startswith("["):
        return None

    parts = [p.strip() for p in cleaned.split(",")]
    if len(parts) != EXPECTED_FEATURE_COUNT:
        return None

    try:
        values = [float(p) for p in parts]
    except ValueError:
        return None

    if any(not math.isfinite(v) for v in values):
        return None

    return values


class SerialReader(threading.Thread):
    def __init__(self, port: str, baud: int, out_queue: queue.Queue[Sample]):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.out_queue = out_queue
        self.stop_event = threading.Event()
        self.error: Optional[str] = None
        self.valid_count = 0
        self.invalid_count = 0

    def run(self) -> None:
        try:
            with serial.Serial(self.port, self.baud, timeout=0.05) as ser:
                while not self.stop_event.is_set():
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore")
                    values = parse_sample_line(line)
                    if values is None:
                        self.invalid_count += 1
                        continue

                    self.valid_count += 1
                    sample = Sample(timestamp=time.time(), values=values)
                    try:
                        self.out_queue.put_nowait(sample)
                    except queue.Full:
                        # Drop oldest one to keep the stream fresh.
                        try:
                            self.out_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self.out_queue.put_nowait(sample)
        except serial.SerialException as exc:
            self.error = f"Serial error: {exc}"
        except Exception as exc:  # Defensive guard for production use.
            self.error = f"Unexpected reader error: {exc}"

    def stop(self) -> None:
        self.stop_event.set()


class KeyReader:
    """Cross-platform non-blocking single-key reader."""

    def __init__(self) -> None:
        self._is_windows = os.name == "nt"
        self._fd = None
        self._old_term = None

    def __enter__(self) -> "KeyReader":
        if not self._is_windows:
            import termios
            import tty

            self._fd = sys.stdin.fileno()
            self._old_term = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._is_windows and self._fd is not None and self._old_term is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_term)

    def get_key(self) -> Optional[str]:
        if self._is_windows:
            import msvcrt

            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            # Skip special multi-byte key prefixes.
            if ch in ("\x00", "\xe0"):
                _ = msvcrt.getwch()
                return None
            return "SPACE" if ch == " " else ch

        import select

        if self._fd is None:
            return None
        readable, _, _ = select.select([sys.stdin], [], [], 0)
        if not readable:
            return None
        ch = sys.stdin.read(1)
        return "SPACE" if ch == " " else ch


class LivePlotter:
    def __init__(self, history_seconds: float, target_hz: float) -> None:
        import matplotlib.pyplot as plt

        self._plt = plt
        self.history_seconds = max(2.0, history_seconds)
        max_samples = int(self.history_seconds * max(5.0, target_hz))

        self.t = deque(maxlen=max_samples)
        self.buffers = [deque(maxlen=max_samples) for _ in range(EXPECTED_FEATURE_COUNT)]

        self.fig, self.axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
        self.groups = [
            (0, 1, 2, "Acceleration"),
            (3, 4, 5, "Gyroscope"),
            (6, 7, 8, "Yaw / Pitch / Roll"),
        ]

        self.lines = []
        for ax, (i0, i1, i2, title) in zip(self.axes, self.groups):
            ln0, = ax.plot([], [], label=FEATURE_NAMES[i0])
            ln1, = ax.plot([], [], label=FEATURE_NAMES[i1])
            ln2, = ax.plot([], [], label=FEATURE_NAMES[i2])
            ax.set_title(title)
            ax.legend(loc="upper right")
            ax.grid(True, alpha=0.25)
            self.lines.append((ln0, ln1, ln2))

        self.axes[-1].set_xlabel("Time (s, recent window)")
        self.last_draw = 0.0

        plt.ion()
        plt.tight_layout()
        plt.show(block=False)

    def update(self, sample: Sample) -> None:
        self.t.append(sample.timestamp)
        for i, v in enumerate(sample.values):
            self.buffers[i].append(v)

        now = time.time()
        if now - self.last_draw < 0.08:
            return
        self.last_draw = now

        if len(self.t) < 2:
            return

        t_last = self.t[-1]
        t_rel = [t - t_last for t in self.t]

        for ax, (i0, i1, i2, _), (ln0, ln1, ln2) in zip(self.axes, self.groups, self.lines):
            y0 = list(self.buffers[i0])
            y1 = list(self.buffers[i1])
            y2 = list(self.buffers[i2])
            ln0.set_data(t_rel, y0)
            ln1.set_data(t_rel, y1)
            ln2.set_data(t_rel, y2)

            y_all = y0 + y1 + y2
            y_min, y_max = min(y_all), max(y_all)
            pad = max(1e-3, (y_max - y_min) * 0.12)
            ax.set_xlim(-self.history_seconds, 0.0)
            ax.set_ylim(y_min - pad, y_max + pad)

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

    def close(self) -> None:
        self._plt.ioff()
        self._plt.close(self.fig)


def choose_port(explicit_port: Optional[str]) -> str:
    if explicit_port:
        return explicit_port

    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        raise RuntimeError("No serial ports found. Pass --port explicitly.")

    print(f"Auto-selected serial port: {ports[0]}")
    return ports[0]


def write_recording(out_dir: Path, index: int, rows: list[list[object]]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    first_label = rows[0][-1] if rows else "unknown"
    file_path = out_dir / f"record_{index:03d}_{first_label}_{ts}.csv"

    with file_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", *FEATURE_NAMES, "label"])
        writer.writerows(rows)

    return file_path


def print_controls() -> None:
    print("\nControls:")
    print("  1=swipe, 2=shake, 3=circle, 4=wave, 5=idle")
    print("  space=start/stop recording")
    print("  q=quit\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Record MPU6050 serial samples to labeled CSV files.")
    parser.add_argument("--port", help="Serial port (e.g., COM5). Auto-detected if omitted.")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate (default: 115200).")
    parser.add_argument("--out", default="recordings", help="Output directory for CSV files.")
    parser.add_argument("--target-hz", type=float, default=50.0, help="Approximate saved sample rate.")
    parser.add_argument("--plot", action="store_true", help="Enable optional live plotting.")
    parser.add_argument("--plot-history", type=float, default=10.0, help="Live plot history window in seconds.")
    parser.add_argument("--list-ports", action="store_true", help="List serial ports and exit.")
    args = parser.parse_args()

    if args.list_ports:
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            print("No serial ports found.")
            return 0
        for p in ports:
            print(f"{p.device} - {p.description}")
        return 0

    try:
        port = choose_port(args.port)
    except RuntimeError as exc:
        print(exc)
        return 1

    out_dir = Path(args.out)
    sample_queue: queue.Queue[Sample] = queue.Queue(maxsize=4096)
    reader = SerialReader(port=port, baud=args.baud, out_queue=sample_queue)

    plotter = None
    if args.plot:
        try:
            plotter = LivePlotter(history_seconds=args.plot_history, target_hz=args.target_hz)
        except Exception as exc:
            print(f"Live plotting disabled ({exc}). Install matplotlib to enable --plot.")

    print(f"Listening on {port} @ {args.baud} baud")
    print_controls()

    current_label = "idle"
    recording = False
    record_rows: list[list[object]] = []
    record_index = 1
    kept_count = 0
    last_kept_ts = 0.0
    min_period = 1.0 / args.target_hz if args.target_hz > 0 else 0.0
    last_status_print = 0.0

    reader.start()

    try:
        with KeyReader() as keys:
            while True:
                key = keys.get_key()
                if key:
                    key = key.lower()
                    if key in LABEL_KEYS:
                        current_label = LABEL_KEYS[key]
                        print(f"Label set to: {current_label}")
                    elif key == "space":
                        if not recording:
                            recording = True
                            record_rows = []
                            print("Recording started")
                        else:
                            recording = False
                            if record_rows:
                                file_path = write_recording(out_dir, record_index, record_rows)
                                print(f"Recording stopped -> saved {len(record_rows)} rows to {file_path}")
                                record_index += 1
                            else:
                                print("Recording stopped (no valid samples captured)")
                    elif key == "q":
                        print("Quit requested")
                        break

                # Drain queue quickly each loop to keep up with serial data.
                for _ in range(256):
                    try:
                        sample = sample_queue.get_nowait()
                    except queue.Empty:
                        break

                    if min_period > 0 and (sample.timestamp - last_kept_ts) < min_period:
                        continue

                    last_kept_ts = sample.timestamp
                    kept_count += 1

                    if recording:
                        row = [
                            f"{sample.timestamp:.6f}",
                            *[f"{v:.6f}" for v in sample.values],
                            current_label,
                        ]
                        record_rows.append(row)

                    if plotter is not None:
                        plotter.update(sample)

                if reader.error:
                    print(reader.error)
                    break

                now = time.time()
                if now - last_status_print > 1.0:
                    rec_state = "ON" if recording else "OFF"
                    print(
                        f"status: rec={rec_state} label={current_label} "
                        f"valid={reader.valid_count} invalid={reader.invalid_count} kept={kept_count}",
                        end="\r",
                        flush=True,
                    )
                    last_status_print = now

                time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        print()
        reader.stop()
        reader.join(timeout=2.0)

        if recording and record_rows:
            file_path = write_recording(out_dir, record_index, record_rows)
            print(f"Auto-saved active recording ({len(record_rows)} rows) to {file_path}")

        if plotter is not None:
            plotter.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
