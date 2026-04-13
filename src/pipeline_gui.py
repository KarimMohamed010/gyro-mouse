#!/usr/bin/env python3
"""
Unified GUI for MPU6050 gesture pipeline:
1) Record labeled gesture CSVs
2) Train models (LSTM + CNN)
3) Deploy model/scaler artifacts to firmware sources
"""

from __future__ import annotations

import csv
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import serial
import serial.tools.list_ports

FEATURE_NAMES = ["ax", "ay", "az", "gx", "gy", "gz", "yaw", "pitch", "roll"]
EXPECTED_FEATURE_COUNT = 9
LABELS = ["swipe", "shake", "circle", "wave", "idle"]


@dataclass
class Sample:
    timestamp: float
    values: list[float]


class SerialStreamThread(threading.Thread):
    def __init__(self, port: str, baud: int, sample_queue: queue.Queue[Sample], error_queue: queue.Queue[str]):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.sample_queue = sample_queue
        self.error_queue = error_queue
        self.stop_event = threading.Event()

    @staticmethod
    def parse_sample_line(line: str) -> Optional[list[float]]:
        cleaned = line.strip()
        if not cleaned:
            return None

        if cleaned.startswith("{") or cleaned.startswith("[") or cleaned.startswith("ml_gestures,"):
            return None

        parts = [p.strip() for p in cleaned.split(",")]
        if len(parts) != EXPECTED_FEATURE_COUNT:
            return None

        try:
            values = [float(p) for p in parts]
        except ValueError:
            return None

        return values

    def run(self) -> None:
        try:
            with serial.Serial(self.port, self.baud, timeout=0.05) as ser:
                while not self.stop_event.is_set():
                    raw = ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="ignore")
                    values = self.parse_sample_line(line)
                    if values is None:
                        continue

                    sample = Sample(timestamp=time.time(), values=values)
                    try:
                        self.sample_queue.put_nowait(sample)
                    except queue.Full:
                        try:
                            self.sample_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self.sample_queue.put_nowait(sample)
        except Exception as exc:
            self.error_queue.put(f"Serial stream error: {exc}")

    def stop(self) -> None:
        self.stop_event.set()


class ProcessRunner:
    def __init__(self, on_log):
        self.on_log = on_log
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None

    def start(self, cmd: list[str], cwd: Path) -> bool:
        if self.process is not None and self.process.poll() is None:
            self.on_log("Another process is already running. Stop it first.\n")
            return False

        self.on_log(f"\n$ {' '.join(cmd)}\n")
        self.process = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _reader():
            assert self.process is not None
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.on_log(line)
            code = self.process.wait()
            self.on_log(f"\n[process exited with code {code}]\n")

        self.thread = threading.Thread(target=_reader, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.on_log("\n[termination requested]\n")


class PipelineGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MPU6050 Gesture Pipeline GUI")
        self.geometry("1100x760")

        self.script_dir = Path(__file__).resolve().parent
        self.py = sys.executable

        self.sample_queue: queue.Queue[Sample] = queue.Queue(maxsize=4096)
        self.error_queue: queue.Queue[str] = queue.Queue(maxsize=32)
        self.serial_thread: Optional[SerialStreamThread] = None

        self.current_label = tk.StringVar(value="idle")
        self.recording = False
        self.record_rows: list[list[str]] = []
        self.record_index = 1
        self.samples_seen = 0

        self.train_log_queue: queue.Queue[str] = queue.Queue(maxsize=10000)
        self.deploy_log_queue: queue.Queue[str] = queue.Queue(maxsize=10000)
        self.train_runner = ProcessRunner(lambda s: self._queue_log(self.train_log_queue, s))
        self.deploy_runner = ProcessRunner(lambda s: self._queue_log(self.deploy_log_queue, s))

        self._build_ui()
        self._refresh_ports()
        self._ui_pump()

    def _build_ui(self):
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.record_tab = tk.Frame(notebook)
        self.train_tab = tk.Frame(notebook)
        self.deploy_tab = tk.Frame(notebook)

        notebook.add(self.record_tab, text="1) Record")
        notebook.add(self.train_tab, text="2) Train")
        notebook.add(self.deploy_tab, text="3) Deploy")

        self._build_record_tab()
        self._build_train_tab()
        self._build_deploy_tab()

    def _build_record_tab(self):
        top = tk.LabelFrame(self.record_tab, text="Serial Input")
        top.pack(fill="x", padx=8, pady=8)

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")

        tk.Label(top, text="Port").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=16, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=5, pady=5)

        tk.Button(top, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, padx=5, pady=5)

        tk.Label(top, text="Baud").grid(row=0, column=3, sticky="w", padx=5, pady=5)
        tk.Entry(top, textvariable=self.baud_var, width=10).grid(row=0, column=4, padx=5, pady=5)

        tk.Button(top, text="Connect Stream", command=self._connect_stream).grid(row=0, column=5, padx=5, pady=5)
        tk.Button(top, text="Disconnect", command=self._disconnect_stream).grid(row=0, column=6, padx=5, pady=5)

        out = tk.LabelFrame(self.record_tab, text="Recording")
        out.pack(fill="x", padx=8, pady=8)

        self.record_dir_var = tk.StringVar(value=str(self.script_dir / "recordings"))
        tk.Label(out, text="Output Folder").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(out, textvariable=self.record_dir_var, width=70).grid(row=0, column=1, padx=5, pady=5)
        tk.Button(out, text="Browse", command=self._browse_record_dir).grid(row=0, column=2, padx=5, pady=5)

        labels_frame = tk.LabelFrame(self.record_tab, text="Current Label")
        labels_frame.pack(fill="x", padx=8, pady=8)
        for i, label in enumerate(LABELS):
            tk.Radiobutton(labels_frame, text=label, value=label, variable=self.current_label).grid(
                row=0, column=i, padx=10, pady=6, sticky="w"
            )

        actions = tk.Frame(self.record_tab)
        actions.pack(fill="x", padx=8, pady=8)
        tk.Button(actions, text="Start Recording", command=self._start_recording, bg="#2e7d32", fg="white").pack(side="left", padx=6)
        tk.Button(actions, text="Stop Recording", command=self._stop_recording, bg="#b71c1c", fg="white").pack(side="left", padx=6)

        self.record_status = tk.StringVar(value="Disconnected. Not recording.")
        tk.Label(actions, textvariable=self.record_status).pack(side="left", padx=12)

        stats = tk.LabelFrame(self.record_tab, text="Live Stats")
        stats.pack(fill="x", padx=8, pady=8)
        self.samples_var = tk.StringVar(value="Samples seen: 0")
        self.last_sample_var = tk.StringVar(value="Last sample: -")
        tk.Label(stats, textvariable=self.samples_var).pack(anchor="w", padx=8, pady=4)
        tk.Label(stats, textvariable=self.last_sample_var, font=("Consolas", 9)).pack(anchor="w", padx=8, pady=4)

    def _build_train_tab(self):
        cfg = tk.LabelFrame(self.train_tab, text="Training Config")
        cfg.pack(fill="x", padx=8, pady=8)

        self.train_data_dir_var = tk.StringVar(value=str(self.script_dir / "recordings"))
        self.train_out_dir_var = tk.StringVar(value=str(self.script_dir / "training_artifacts"))
        self.epochs_var = tk.StringVar(value="40")
        self.batch_var = tk.StringVar(value="64")

        tk.Label(cfg, text="Data Folder").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.train_data_dir_var, width=70).grid(row=0, column=1, padx=5, pady=5)
        tk.Button(cfg, text="Browse", command=self._browse_train_data).grid(row=0, column=2, padx=5, pady=5)

        tk.Label(cfg, text="Output Folder").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.train_out_dir_var, width=70).grid(row=1, column=1, padx=5, pady=5)
        tk.Button(cfg, text="Browse", command=self._browse_train_out).grid(row=1, column=2, padx=5, pady=5)

        tk.Label(cfg, text="Epochs").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.epochs_var, width=10).grid(row=2, column=1, sticky="w", padx=5, pady=5)

        tk.Label(cfg, text="Batch Size").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.batch_var, width=10).grid(row=3, column=1, sticky="w", padx=5, pady=5)

        actions = tk.Frame(self.train_tab)
        actions.pack(fill="x", padx=8, pady=8)
        tk.Button(actions, text="Run Training", command=self._run_training, bg="#1a237e", fg="white").pack(side="left", padx=6)
        tk.Button(actions, text="Stop", command=self.train_runner.stop).pack(side="left", padx=6)

        log_frame = tk.LabelFrame(self.train_tab, text="Training Log")
        log_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.train_log = tk.Text(log_frame, wrap="word")
        self.train_log.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_deploy_tab(self):
        cfg = tk.LabelFrame(self.deploy_tab, text="Model/Scaler Deployment")
        cfg.pack(fill="x", padx=8, pady=8)

        self.model_path_var = tk.StringVar(value=str(self.script_dir / "training_artifacts" / "cnn1d" / "cnn1d_int8.tflite"))
        self.symbol_var = tk.StringVar(value="g_gesture_model_data")
        self.deploy_out_dir_var = tk.StringVar(value=str(self.script_dir))

        self.scaler_path_var = tk.StringVar(value=str(self.script_dir / "training_artifacts" / "preprocessing" / "scaler.joblib"))
        self.norm_header_var = tk.StringVar(value=str(self.script_dir / "feature_norm.h"))

        tk.Label(cfg, text="TFLite Model").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.model_path_var, width=70).grid(row=0, column=1, padx=5, pady=5)
        tk.Button(cfg, text="Browse", command=self._browse_model_file).grid(row=0, column=2, padx=5, pady=5)

        tk.Label(cfg, text="C Symbol").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.symbol_var, width=28).grid(row=1, column=1, sticky="w", padx=5, pady=5)

        tk.Label(cfg, text="Output Dir").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.deploy_out_dir_var, width=70).grid(row=2, column=1, padx=5, pady=5)
        tk.Button(cfg, text="Browse", command=self._browse_deploy_out).grid(row=2, column=2, padx=5, pady=5)

        tk.Label(cfg, text="Scaler (.joblib)").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.scaler_path_var, width=70).grid(row=3, column=1, padx=5, pady=5)
        tk.Button(cfg, text="Browse", command=self._browse_scaler_file).grid(row=3, column=2, padx=5, pady=5)

        tk.Label(cfg, text="feature_norm.h").grid(row=4, column=0, sticky="w", padx=5, pady=5)
        tk.Entry(cfg, textvariable=self.norm_header_var, width=70).grid(row=4, column=1, padx=5, pady=5)
        tk.Button(cfg, text="Browse", command=self._browse_norm_header).grid(row=4, column=2, padx=5, pady=5)

        actions = tk.Frame(self.deploy_tab)
        actions.pack(fill="x", padx=8, pady=8)
        tk.Button(actions, text="Convert Model -> C Array", command=self._convert_model, bg="#004d40", fg="white").pack(side="left", padx=6)
        tk.Button(actions, text="Export Scaler Header", command=self._export_scaler, bg="#00695c", fg="white").pack(side="left", padx=6)
        tk.Button(actions, text="Run Both", command=self._run_deploy_both, bg="#00796b", fg="white").pack(side="left", padx=6)
        tk.Button(actions, text="Stop", command=self.deploy_runner.stop).pack(side="left", padx=6)

        log_frame = tk.LabelFrame(self.deploy_tab, text="Deploy Log")
        log_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.deploy_log = tk.Text(log_frame, wrap="word")
        self.deploy_log.pack(fill="both", expand=True, padx=6, pady=6)

    def _queue_log(self, q: queue.Queue[str], text: str):
        try:
            q.put_nowait(text)
        except queue.Full:
            pass

    def _drain_log_queue(self, q: queue.Queue[str], widget: tk.Text):
        changed = False
        while True:
            try:
                line = q.get_nowait()
            except queue.Empty:
                break
            widget.insert("end", line)
            changed = True
        if changed:
            widget.see("end")

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _browse_record_dir(self):
        path = filedialog.askdirectory(initialdir=self.record_dir_var.get())
        if path:
            self.record_dir_var.set(path)

    def _browse_train_data(self):
        path = filedialog.askdirectory(initialdir=self.train_data_dir_var.get())
        if path:
            self.train_data_dir_var.set(path)

    def _browse_train_out(self):
        path = filedialog.askdirectory(initialdir=self.train_out_dir_var.get())
        if path:
            self.train_out_dir_var.set(path)

    def _browse_model_file(self):
        path = filedialog.askopenfilename(initialdir=self.script_dir, filetypes=[("TFLite", "*.tflite"), ("All", "*.*")])
        if path:
            self.model_path_var.set(path)

    def _browse_scaler_file(self):
        path = filedialog.askopenfilename(initialdir=self.script_dir, filetypes=[("Joblib", "*.joblib"), ("All", "*.*")])
        if path:
            self.scaler_path_var.set(path)

    def _browse_deploy_out(self):
        path = filedialog.askdirectory(initialdir=self.deploy_out_dir_var.get())
        if path:
            self.deploy_out_dir_var.set(path)

    def _browse_norm_header(self):
        path = filedialog.asksaveasfilename(initialdir=self.script_dir, defaultextension=".h", filetypes=[("Header", "*.h")])
        if path:
            self.norm_header_var.set(path)

    def _connect_stream(self):
        if self.serial_thread and self.serial_thread.is_alive():
            messagebox.showinfo("Serial", "Stream already connected.")
            return

        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("Serial", "Choose a serial port first.")
            return

        try:
            baud = int(self.baud_var.get().strip())
        except ValueError:
            messagebox.showwarning("Serial", "Baud must be an integer.")
            return

        self.serial_thread = SerialStreamThread(port=port, baud=baud, sample_queue=self.sample_queue, error_queue=self.error_queue)
        self.serial_thread.start()
        self.record_status.set(f"Connected to {port} @ {baud}. Not recording.")

    def _disconnect_stream(self):
        if self.serial_thread:
            self.serial_thread.stop()
            self.serial_thread = None
        self.record_status.set("Disconnected. Not recording.")

    def _start_recording(self):
        if not (self.serial_thread and self.serial_thread.is_alive()):
            messagebox.showwarning("Record", "Connect serial stream first.")
            return
        self.recording = True
        self.record_rows = []
        self.record_status.set(f"Recording... label={self.current_label.get()}")

    def _stop_recording(self):
        if not self.recording:
            return

        self.recording = False
        rows = self.record_rows
        self.record_rows = []

        if not rows:
            self.record_status.set("Stopped. No samples captured.")
            return

        out_dir = Path(self.record_dir_var.get())
        out_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        label = rows[0][-1]
        out_file = out_dir / f"record_{self.record_index:03d}_{label}_{ts}.csv"

        with out_file.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", *FEATURE_NAMES, "label"])
            writer.writerows(rows)

        self.record_index += 1
        self.record_status.set(f"Saved {len(rows)} rows -> {out_file.name}")

    def _run_training(self):
        data_dir = self.train_data_dir_var.get().strip()
        out_dir = self.train_out_dir_var.get().strip()
        epochs = self.epochs_var.get().strip()
        batch = self.batch_var.get().strip()

        cmd = [
            self.py,
            str(self.script_dir / "train_gesture_models.py"),
            "--data-dir",
            data_dir,
            "--out-dir",
            out_dir,
            "--epochs",
            epochs,
            "--batch-size",
            batch,
        ]
        self.train_runner.start(cmd, cwd=self.script_dir)

    def _convert_model(self):
        cmd = [
            self.py,
            str(self.script_dir / "tflite_to_c_array.py"),
            "--input",
            self.model_path_var.get().strip(),
            "--out-dir",
            self.deploy_out_dir_var.get().strip(),
            "--symbol",
            self.symbol_var.get().strip(),
            "--header",
            "model_data.h",
            "--source",
            "model_data.cpp",
        ]
        self.deploy_runner.start(cmd, cwd=self.script_dir)

    def _export_scaler(self):
        cmd = [
            self.py,
            str(self.script_dir / "export_scaler_header.py"),
            "--scaler",
            self.scaler_path_var.get().strip(),
            "--out",
            self.norm_header_var.get().strip(),
        ]
        self.deploy_runner.start(cmd, cwd=self.script_dir)

    def _run_deploy_both(self):
        model_cmd = [
            self.py,
            str(self.script_dir / "tflite_to_c_array.py"),
            "--input",
            self.model_path_var.get().strip(),
            "--out-dir",
            self.deploy_out_dir_var.get().strip(),
            "--symbol",
            self.symbol_var.get().strip(),
            "--header",
            "model_data.h",
            "--source",
            "model_data.cpp",
        ]

        scaler_cmd = [
            self.py,
            str(self.script_dir / "export_scaler_header.py"),
            "--scaler",
            self.scaler_path_var.get().strip(),
            "--out",
            self.norm_header_var.get().strip(),
        ]

        if not self.deploy_runner.start(model_cmd, cwd=self.script_dir):
            return

        def _run_second_when_done():
            while self.deploy_runner.process and self.deploy_runner.process.poll() is None:
                time.sleep(0.1)
            self.deploy_runner.start(scaler_cmd, cwd=self.script_dir)

        threading.Thread(target=_run_second_when_done, daemon=True).start()

    def _ui_pump(self):
        while True:
            try:
                sample = self.sample_queue.get_nowait()
            except queue.Empty:
                break

            self.samples_seen += 1
            self.samples_var.set(f"Samples seen: {self.samples_seen}")
            sample_str = ", ".join(f"{v:.2f}" for v in sample.values)
            self.last_sample_var.set(f"Last sample: {sample_str}")

            if self.recording:
                row = [
                    f"{sample.timestamp:.6f}",
                    *[f"{v:.6f}" for v in sample.values],
                    self.current_label.get(),
                ]
                self.record_rows.append(row)

        while True:
            try:
                err = self.error_queue.get_nowait()
            except queue.Empty:
                break
            self.record_status.set(err)

        self._drain_log_queue(self.train_log_queue, self.train_log)
        self._drain_log_queue(self.deploy_log_queue, self.deploy_log)

        self.after(30, self._ui_pump)

    def destroy(self):
        if self.serial_thread:
            self.serial_thread.stop()
        self.train_runner.stop()
        self.deploy_runner.stop()
        super().destroy()


def main() -> int:
    app = PipelineGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
