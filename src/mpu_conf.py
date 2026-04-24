#!/usr/bin/env python3
"""
ESP32 MPU Mouse — Axis Calibration GUI (BLE Version)
Requirements: pip install bleak
"""

import json, threading, time, asyncio, struct
import tkinter as tk
from tkinter import ttk, messagebox
from bleak import BleakClient, BleakScanner

# ─────────────────────────────────────────────────────────────────────────────
AXIS_NAMES  = ["Yaw (0)", "Pitch (1)", "Roll (2)"]
SAVE_FILE   = "mpu_config.json"

DEFAULT_CFG = {
    "cursorXAxis": 0, "cursorYAxis": 2, "clickAxis": 1,
    "invertX": False, "invertY": False, "invertClick": False,
    "deadzoneX": 1.5, "deadzoneY": 1.5, "deadzoneClick": 2.0,
    "gainX": 0.3, "gainY": 0.3,
    "tiltThreshDeg": 30.0,
    "flickVelThresh": 120.0, "flickReturnDeg": 8.0, "flickConfirmMs": 300,
    "shakeVelThresh": 60.0, "doubleTiltDeg": 25.0, "circleMinSpeed": 20.0,
    "enableFlick": True, "enableShake": True,
    "enableDoubleTilt": True, "enableCircle": True,
}

C = {
    "bg0":    "#0d0f12", "bg1": "#13161b", "bg2": "#1a1e25", "bg3": "#222733",
    "border": "#2a3040", "border2": "#3a4558",
    "accent": "#00d4ff", "accent2": "#0099bb",
    "green":  "#00e676", "amber": "#ffab40", "red": "#ff5252", "purple": "#b388ff",
    "text0":  "#e8ecf4", "text1": "#8892a4", "text2": "#4a5568",
}
FONT_MONO  = ("Consolas", 9)
FONT_TINY  = ("Consolas", 8)
FONT_HEAD  = ("Segoe UI", 9, "bold")

# BLE UUIDs
RAW_DATA_CHAR_UUID = "abcd0001-5678-1234-5678-1234567890ab"
GESTURE_CHAR_UUID  = "abcd0002-5678-1234-5678-1234567890ab"
CONFIG_CHAR_UUID   = "abcd0004-5678-1234-5678-1234567890ab"

# ─────────────────────────────────────────────────────────────────────────────
class BleConfigThread(threading.Thread):
    def __init__(self, address, on_data, on_gesture, on_status):
        super().__init__(daemon=True)
        self.address = address
        self.on_data = on_data
        self.on_gesture = on_gesture
        self.on_status = on_status
        self._stop = asyncio.Event()
        self.client = None
        self.connected = False
        self._loop = None
        self._write_queue = asyncio.Queue()

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._run_async())

    async def _run_async(self):
        def raw_data_handler(sender, data):
            if len(data) == 19 and data[0] == 0x01:
                # Unpack 9 int16s (little endian)
                vals = struct.unpack("<9h", data[1:])
                # We only need yaw, pitch, roll which are the last 3, scaled by 100
                yaw, pitch, roll = vals[6] / 100.0, vals[7] / 100.0, vals[8] / 100.0
                self.on_data([yaw, pitch, roll])

        def gesture_handler(sender, data):
            try:
                msg = json.loads(data.decode(errors="ignore"))
                self.on_gesture(msg)
            except json.JSONDecodeError:
                pass

        def config_handler(sender, data):
            try:
                msg = json.loads(data.decode(errors="ignore"))
                if "ack" in msg or "pong" in msg:
                    self.on_status("ack" if "ack" in msg else "connected")
            except json.JSONDecodeError:
                pass

        def on_disconnect(client):
            self.connected = False
            self.on_status("disconnected")
            self._stop.set()

        try:
            self.client = BleakClient(self.address, disconnected_callback=on_disconnect)
            await self.client.connect()
            self.connected = True
            self.on_status("connected")

            await self.client.start_notify(RAW_DATA_CHAR_UUID, raw_data_handler)
            await self.client.start_notify(GESTURE_CHAR_UUID, gesture_handler)
            await self.client.start_notify(CONFIG_CHAR_UUID, config_handler)

            # Send ping
            await self.client.write_gatt_char(CONFIG_CHAR_UUID, b'{"ping":1}')

            while not self._stop.is_set():
                try:
                    # Wait for items to write, timeout to check stop event
                    payload = await asyncio.wait_for(self._write_queue.get(), timeout=0.1)
                    await self.client.write_gatt_char(CONFIG_CHAR_UUID, (json.dumps(payload)).encode())
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    self.on_status(f"error:{e}")

        except Exception as e:
            self.on_status(f"error:{e}")
        finally:
            if self.client and self.client.is_connected:
                await self.client.disconnect()
            self.connected = False

    def send(self, payload):
        if self.connected and self._loop:
            asyncio.run_coroutine_threadsafe(self._write_queue.put(payload), self._loop)

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._stop.set)

# ─────────────────────────────────────────────────────────────────────────────
class PrecisionSlider(tk.Frame):
    """Slider + spinbox for coarse drag AND precise keyboard/type tuning."""

    def __init__(self, parent, from_, to, resolution, initial=None,
                 width=200, on_change=None, **kw):
        super().__init__(parent, bg=C["bg1"], **kw)
        self._from, self._to, self._res = from_, to, resolution
        self._cb       = on_change
        self._updating = False
        self._var      = tk.DoubleVar(value=initial if initial is not None else from_)
        self._var.trace_add("write", self._on_var)

        self._slider = tk.Scale(
            self, from_=from_, to=to, resolution=resolution,
            orient="horizontal", variable=self._var,
            length=width, showvalue=False,
            bg=C["bg1"], troughcolor=C["bg3"],
            activebackground=C["accent"],
            highlightthickness=0, relief="flat",
            sliderlength=12, sliderrelief="flat", bd=0,
        )
        self._slider.pack(side="left")

        vcmd = (self.register(self._validate), "%P")
        self._spin = tk.Spinbox(
            self, from_=from_, to=to, increment=resolution,
            textvariable=self._var, width=7,
            format=f"%.{self._dec()}f",
            validate="key", validatecommand=vcmd,
            bg=C["bg3"], fg=C["text0"], insertbackground=C["accent"],
            buttonbackground=C["bg2"], relief="flat",
            highlightthickness=1, highlightbackground=C["border"],
            highlightcolor=C["accent"], font=FONT_MONO, bd=0,
        )
        self._spin.pack(side="left", padx=(5, 0))

    def _dec(self):
        s = str(self._res)
        return len(s.split(".")[1]) if "." in s else 0

    def _validate(self, P):
        if P in ("", "-", "."): return True
        try: float(P); return True
        except ValueError: return False

    def _on_var(self, *_):
        if self._updating: return
        self._updating = True
        try:
            v = round(float(self._var.get()), self._dec())
            v = max(self._from, min(self._to, v))
            if self._cb: self._cb(v)
        except (tk.TclError, ValueError): pass
        finally: self._updating = False

    def get(self):
        try: return round(float(self._var.get()), self._dec())
        except: return self._from

    def set(self, v):
        self._var.set(round(float(v), self._dec()))

# ─────────────────────────────────────────────────────────────────────────────
class DeadzoneViz(tk.Canvas):
    W = H = 80

    def __init__(self, parent, label, **kw):
        super().__init__(parent, width=self.W, height=self.H,
                         bg=C["bg2"], highlightthickness=0, **kw)
        self._label = label
        self._value = 0.0
        self._dead  = 1.5
        self._draw()

    def set_value(self, v):
        self._value = max(-90.0, min(90.0, v))
        self._draw()

    def set_dead(self, d):
        self._dead = max(0.0, d)
        self._draw()

    def _draw(self):
        self.delete("all")
        cx = cy = self.W // 2
        r  = cx - 8
        self.create_oval(cx-r, cy-r, cx+r, cy+r, outline=C["border2"], fill=C["bg3"])
        dr = int(r * min(1.0, self._dead / 90.0))
        self.create_oval(cx-dr, cy-dr, cx+dr, cy+dr,
                         outline=C["border2"], fill=C["bg2"], dash=(2, 3))
        self.create_line(cx-3, cy, cx+3, cy, fill=C["border2"])
        self.create_line(cx, cy-3, cx, cy+3, fill=C["border2"])
        norm  = self._value / 90.0
        dot_x = cx + int(norm * r * 0.88)
        inside = abs(self._value) < self._dead
        col = C["text2"] if inside else C["green"]
        self.create_oval(dot_x-6, cy-6, dot_x+6, cy+6,
                         fill="#003320" if not inside else C["bg3"], outline="")
        self.create_oval(dot_x-3, cy-3, dot_x+3, cy+3, fill=col, outline="")
        sign = "+" if self._value >= 0 else ""
        self.create_text(cx, self.H-5,
                         text=f"{sign}{self._value:.1f}°",
                         fill=C["text1"], font=FONT_TINY)
        self.create_text(cx, 7, text=self._label,
                         fill=C["accent"] if not inside else C["text2"],
                         font=FONT_TINY)

# ─────────────────────────────────────────────────────────────────────────────
class AxisBar(tk.Canvas):
    W, H = 230, 18

    def __init__(self, parent, **kw):
        super().__init__(parent, width=self.W, height=self.H,
                         bg=C["bg1"], highlightthickness=0, **kw)
        self._value = 0.0
        self._dead  = 0.0
        self._draw()

    def update(self, value, dead=0.0):
        self._value, self._dead = value, dead
        self._draw()

    def _draw(self):
        self.delete("all")
        cx, cy = self.W // 2, self.H // 2
        self.create_rectangle(2, cy-3, self.W-2, cy+3,
                               fill=C["bg3"], outline=C["border"])
        self.create_line(cx, cy-5, cx, cy+5, fill=C["border2"])
        if self._dead > 0:
            dr = int((self._dead / 90.0) * (cx - 4))
            self.create_rectangle(cx-dr, cy-3, cx+dr, cy+3, fill=C["bg2"], outline="")
        norm  = max(-1.0, min(1.0, self._value / 90.0))
        bw    = int(abs(norm) * (cx - 4))
        inside = abs(self._value) <= self._dead
        col = C["text2"] if inside else (C["green"] if abs(self._value) < 45 else C["amber"])
        if norm >= 0:
            self.create_rectangle(cx, cy-3, cx+bw, cy+3, fill=col, outline="")
        else:
            self.create_rectangle(cx-bw, cy-3, cx, cy+3, fill=col, outline="")
        nx = cx + int(norm * (cx - 4))
        self.create_rectangle(nx-1, 2, nx+1, self.H-2, fill=C["accent"], outline="")
        sign = "+" if self._value >= 0 else ""
        self.create_text(self.W-3, cy, text=f"{sign}{self._value:6.1f}°",
                         anchor="e", fill=C["text0"], font=FONT_MONO)

# ─────────────────────────────────────────────────────────────────────────────
class Card(tk.Frame):
    def __init__(self, parent, title, **kw):
        super().__init__(parent, bg=C["bg1"],
                         highlightthickness=1, highlightbackground=C["border"], **kw)
        hdr = tk.Frame(self, bg=C["bg0"])
        hdr.pack(fill="x")
        tk.Frame(hdr, bg=C["accent"], width=3).pack(side="left", fill="y")
        tk.Label(hdr, text=f"  {title}", bg=C["bg0"], fg=C["text0"],
                 font=FONT_HEAD, pady=5).pack(side="left")
        self.body = tk.Frame(self, bg=C["bg1"])
        self.body.pack(fill="both", expand=True, padx=10, pady=8)

# ─────────────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MPU Mouse Config")
        self.configure(bg=C["bg0"])
        self.minsize(880, 620)

        self.cfg    = dict(DEFAULT_CFG)
        self.ble_thread = None
        self.axes   = [0.0, 0.0, 0.0]
        self.device_addresses = {}

        self._load_config()
        self._setup_style()
        self._build_ui()
        self._apply_cfg_to_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._live_loop()

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TCombobox",
                    fieldbackground=C["bg3"], background=C["bg3"],
                    foreground=C["text0"], selectbackground=C["accent2"],
                    bordercolor=C["border"], arrowcolor=C["text1"],
                    relief="flat", padding=(4, 3))
        s.map("TCombobox", fieldbackground=[("readonly", C["bg3"])],
              foreground=[("readonly", C["text0"])])
        s.configure("Vert.TScrollbar", background=C["bg2"],
                    troughcolor=C["bg3"], arrowcolor=C["text1"], relief="flat")

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg=C["bg0"],
                       highlightthickness=1, highlightbackground=C["border"])
        top.pack(fill="x")
        top.columnconfigure(4, weight=1)

        tk.Label(top, text="MPU MOUSE  CONFIG",
                 bg=C["bg0"], fg=C["accent"],
                 font=("Consolas", 11, "bold"), padx=14, pady=8).grid(row=0, column=0)
        tk.Frame(top, bg=C["border"], width=1).grid(row=0, column=1, sticky="ns", pady=4)

        tk.Label(top, text="DEVICE", bg=C["bg0"], fg=C["text1"],
                 font=FONT_TINY, padx=8).grid(row=0, column=2)
        self.device_var = tk.StringVar()
        self.device_cb  = ttk.Combobox(top, textvariable=self.device_var,
                                       width=25, state="readonly")
        self.device_cb.grid(row=0, column=3, padx=(0, 4))
        self._btn(top, "SCAN", self._scan_ble).grid(row=0, column=4, sticky="w", padx=2)
        self._btn(top, "CONNECT", self._connect, accent=True).grid(
            row=0, column=5, padx=6)

        self.led = tk.Canvas(top, width=10, height=10, bg=C["bg0"], highlightthickness=0)
        self.led.grid(row=0, column=6, padx=(12, 3))
        self._led = self.led.create_oval(1, 1, 9, 9, fill=C["red"], outline="")

        self.status_lbl = tk.Label(top, text="DISCONNECTED", bg=C["bg0"],
                                   fg=C["red"], font=("Consolas", 8, "bold"), padx=8)
        self.status_lbl.grid(row=0, column=7, sticky="e")

        # Body
        body = tk.Frame(self, bg=C["bg0"])
        body.pack(fill="both", expand=True, padx=8, pady=8)
        body.columnconfigure(0, weight=3, minsize=520)
        body.columnconfigure(1, weight=2, minsize=300)
        body.rowconfigure(0, weight=1)

        left  = tk.Frame(body, bg=C["bg0"])
        right = tk.Frame(body, bg=C["bg0"])
        left.grid( row=0, column=0, sticky="nsew", padx=(0, 4))
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        left.columnconfigure(0, weight=1)
        for r in range(3): left.rowconfigure(r, weight=[0,0,1][r])
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=0)
        right.rowconfigure(1, weight=1)

        # Left cards
        Card(left, "AXIS MAPPING").grid(row=0, column=0, sticky="ew", pady=(0,5))
        self._build_mapping(left.winfo_children()[-1].body)

        Card(left, "DEADZONES  &  SENSITIVITY").grid(row=1, column=0, sticky="ew", pady=(0,5))
        self._build_deadzones(left.winfo_children()[-1].body)

        Card(left, "GESTURE THRESHOLDS").grid(row=2, column=0, sticky="nsew")
        self._build_gestures(left.winfo_children()[-1].body)

        # Right cards
        Card(right, "LIVE MONITOR").grid(row=0, column=0, sticky="ew", pady=(0,5))
        self._build_live(right.winfo_children()[-1].body)

        Card(right, "GESTURE LOG").grid(row=1, column=0, sticky="nsew")
        self._build_log(right.winfo_children()[-1].body)

        # Bottom bar
        bot = tk.Frame(self, bg=C["bg0"],
                       highlightthickness=1, highlightbackground=C["border"])
        bot.pack(fill="x")
        inner = tk.Frame(bot, bg=C["bg0"])
        inner.pack(side="right", padx=10, pady=6)
        for text, cmd, acc in [
            ("APPLY",           self._apply,         True),
            ("SAVE",            self._save_config,   False),
            ("LOAD",            self._load_and_apply,False),
            ("RESET DEFAULTS",  self._reset_defaults,False),
        ]:
            self._btn(inner, text, cmd, accent=acc).pack(side="left", padx=3)

    # ── Section builders ──────────────────────────────────────────────────────
    def _build_mapping(self, p):
        self.axis_vars, self.invert_vars = {}, {}
        rows = [("cursorXAxis","CURSOR X","invertX","INV X"),
                ("cursorYAxis","CURSOR Y","invertY","INV Y"),
                ("clickAxis",  "CLICK",   "invertClick","INV CLK")]
        for r,(ak,al,ik,il) in enumerate(rows):
            tk.Label(p, text=al, bg=C["bg1"], fg=C["text1"],
                     font=FONT_TINY, width=9, anchor="w").grid(
                         row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            ttk.Combobox(p, textvariable=var, values=AXIS_NAMES,
                         width=14, state="readonly").grid(
                             row=r, column=1, sticky="w", padx=(4,16))
            self.axis_vars[ak] = var
            ivar = tk.BooleanVar()
            tk.Checkbutton(p, text=il, variable=ivar,
                           bg=C["bg1"], fg=C["text1"], selectcolor=C["bg3"],
                           activebackground=C["bg1"], font=FONT_TINY,
                           relief="flat", bd=0, highlightthickness=0).grid(
                               row=r, column=2, sticky="w")
            self.invert_vars[ik] = ivar

    def _build_deadzones(self, p):
        self.dead_sliders, self.dead_vizzes, self.gain_sliders = {}, {}, {}

        viz_row = tk.Frame(p, bg=C["bg1"])
        viz_row.pack(fill="x", pady=(0, 6))
        for key, lbl in [("deadzoneX","X"),("deadzoneY","Y"),("deadzoneClick","CLK")]:
            cell = tk.Frame(viz_row, bg=C["bg1"])
            cell.pack(side="left", padx=10)
            v = DeadzoneViz(cell, lbl)
            v.pack()
            sl = PrecisionSlider(cell, 0.0, 20.0, 0.1,
                                 initial=self.cfg.get(key, 1.5), width=70,
                                 on_change=lambda val, k=key: self._on_dead(k, val))
            sl.pack(pady=(3,0))
            self.dead_vizzes[key] = v
            self.dead_sliders[key] = sl

        tk.Frame(p, bg=C["border"], height=1).pack(fill="x", pady=4)

        for key, lbl in [("gainX","GAIN X"),("gainY","GAIN Y")]:
            row = tk.Frame(p, bg=C["bg1"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=lbl, bg=C["bg1"], fg=C["text1"],
                     font=FONT_TINY, width=9, anchor="w").pack(side="left")
            sl = PrecisionSlider(row, 0.05, 3.0, 0.05,
                                 initial=self.cfg.get(key, 0.3), width=160)
            sl.pack(side="left", padx=4)
            self.gain_sliders[key] = sl

    def _build_gestures(self, p):
        p.columnconfigure(0, weight=1)
        self.thresh_sliders = {}

        # Per-gesture enable switches
        self.gesture_toggle_vars = {}
        toggles = tk.Frame(p, bg=C["bg1"])
        toggles.pack(fill="x", pady=(0, 6))
        toggles.columnconfigure(0, weight=1)
        toggles.columnconfigure(1, weight=1)
        toggle_specs = [
            ("enableFlick", "Enable Flick"),
            ("enableShake", "Enable Shake"),
            ("enableDoubleTilt", "Enable Double-Tilt"),
            ("enableCircle", "Enable Circle"),
        ]
        for i, (key, label) in enumerate(toggle_specs):
            var = tk.BooleanVar(value=self.cfg.get(key, True))
            cb = tk.Checkbutton(
                toggles, text=label, variable=var,
                bg=C["bg1"], fg=C["text1"], selectcolor=C["bg3"],
                activebackground=C["bg1"], font=FONT_TINY,
                relief="flat", bd=0, highlightthickness=0,
            )
            cb.grid(row=i // 2, column=i % 2, sticky="w", pady=1, padx=(0, 8))
            self.gesture_toggle_vars[key] = var

        tk.Frame(p, bg=C["border"], height=1).pack(fill="x", pady=(2, 6))

        specs = [
            ("tiltThreshDeg", "Tilt threshold", "°",    5,  90,  0.5),
            ("flickVelThresh","Flick speed",    "°/s", 40, 400,  5.0),
            ("flickReturnDeg","Flick return",   "°",    1,  30,  0.5),
            ("flickConfirmMs","Flick window",   "ms",  80, 800, 10.0),
            ("shakeVelThresh","Shake speed",    "°/s", 20, 300,  5.0),
            ("doubleTiltDeg", "Double-tilt",    "°",    5,  80,  0.5),
            ("circleMinSpeed","Circle speed",   "°/s",  5, 120,  1.0),
        ]
        for key, lbl, unit, lo, hi, res in specs:
            row = tk.Frame(p, bg=C["bg1"])
            row.pack(fill="x", pady=2)
            row.columnconfigure(1, weight=1)
            tk.Label(row, text=f"{lbl} ({unit})", bg=C["bg1"], fg=C["text1"],
                     font=FONT_TINY, width=22, anchor="w").pack(side="left")
            sl = PrecisionSlider(row, lo, hi, res,
                                 initial=self.cfg.get(key, lo), width=0)
            sl.pack(side="left", fill="x", expand=True)
            self.thresh_sliders[key] = sl

    def _build_live(self, p):
        self.axis_bars = []
        for i, name in enumerate(["YAW", "PITCH", "ROLL"]):
            row = tk.Frame(p, bg=C["bg1"])
            row.pack(fill="x", pady=2)
            row.columnconfigure(1, weight=1)
            tk.Label(row, text=name, bg=C["bg1"], fg=C["accent"],
                     font=("Consolas", 8, "bold"), width=6, anchor="w").pack(side="left")
            bar = AxisBar(row)
            bar.pack(side="left", fill="x", expand=True)
            self.axis_bars.append(bar)

    def _build_log(self, p):
        p.columnconfigure(0, weight=1)
        p.rowconfigure(0, weight=1)
        frame = tk.Frame(p, bg=C["bg1"])
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.gesture_log = tk.Text(
            frame, bg=C["bg0"], fg=C["purple"], font=("Consolas", 9),
            relief="flat", state="disabled", wrap="word", padx=6, pady=4,
            highlightthickness=1, highlightbackground=C["border"],
            insertbackground=C["accent"], selectbackground=C["accent2"],
        )
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.gesture_log.yview)
        self.gesture_log.configure(yscrollcommand=sb.set)
        self.gesture_log.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self._btn(p, "CLEAR LOG", self._clear_log).pack(anchor="e", pady=(4,0))

    # ── Live loop ─────────────────────────────────────────────────────────────
    def _live_loop(self):
        dead_axis = {
            "deadzoneX":     self.cfg.get("cursorXAxis", 0),
            "deadzoneY":     self.cfg.get("cursorYAxis", 2),
            "deadzoneClick": self.cfg.get("clickAxis",   1),
        }
        for i, bar in enumerate(self.axis_bars):
            v = self.axes[i] if i < len(self.axes) else 0.0
            d = next((self.cfg.get(k,0) for k,ax in dead_axis.items() if ax==i), 0.0)
            bar.update(v, d)

        for key, viz in self.dead_vizzes.items():
            ax_key = {"deadzoneX":"cursorXAxis","deadzoneY":"cursorYAxis",
                      "deadzoneClick":"clickAxis"}[key]
            ax = self.cfg.get(ax_key, 0)
            if ax < len(self.axes): viz.set_value(self.axes[ax])

        self.after(50, self._live_loop)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _btn(self, parent, text, cmd, accent=False):
        return tk.Button(parent, text=text, command=cmd,
                         bg=C["accent"] if accent else C["bg3"],
                         fg=C["bg0"]    if accent else C["text1"],
                         activebackground=C["accent2"], activeforeground=C["bg0"],
                         font=("Consolas", 8, "bold"), relief="flat",
                         padx=8, pady=4, cursor="hand2", bd=0,
                         highlightthickness=1, highlightbackground=C["border"])

    def _on_dead(self, key, val):
        self.dead_vizzes[key].set_dead(val)

    # ── Widget ↔ cfg ──────────────────────────────────────────────────────────
    def _apply_cfg_to_widgets(self):
        for k, v in self.axis_vars.items():
            v.set(AXIS_NAMES[min(self.cfg.get(k,0), len(AXIS_NAMES)-1)])
        for k, v in self.invert_vars.items():
            v.set(self.cfg.get(k, False))
        for k, v in self.gesture_toggle_vars.items():
            v.set(self.cfg.get(k, True))
        for k, sl in self.dead_sliders.items():
            val = self.cfg.get(k, 1.5); sl.set(val); self.dead_vizzes[k].set_dead(val)
        for k, sl in self.gain_sliders.items():
            sl.set(self.cfg.get(k, 0.3))
        for k, sl in self.thresh_sliders.items():
            sl.set(self.cfg.get(k, sl.get()))

    def _widgets_to_cfg(self):
        for k, v in self.axis_vars.items():
            try: self.cfg[k] = AXIS_NAMES.index(v.get())
            except ValueError: pass
        for k, v in self.invert_vars.items():  self.cfg[k] = v.get()
        for k, v in self.gesture_toggle_vars.items(): self.cfg[k] = v.get()
        for k, sl in self.dead_sliders.items(): self.cfg[k] = sl.get()
        for k, sl in self.gain_sliders.items(): self.cfg[k] = sl.get()
        for k, sl in self.thresh_sliders.items(): self.cfg[k] = sl.get()

    # ── BLE callbacks ──────────────────────────────────────────────────────
    def _on_data(self, axes):     self.axes = axes
    def _on_gesture(self, msg):   self.after(0, lambda: self._log_gesture(msg))
    def _on_status(self, code):   self.after(0, lambda: self._set_status(code))

    def _set_status(self, code):
        if   code == "connected":    text, col = "CONNECTED",     C["green"]
        elif code == "ack":          text, col = "CFG APPLIED ✓", C["green"]
        elif code == "no_response":  text, col = "NO RESPONSE",   C["amber"]
        elif code.startswith("error:"): text, col = "ERROR",      C["red"]
        else:                        text, col = "DISCONNECTED",  C["red"]
        self.status_lbl.configure(text=text, fg=col)
        self.led.itemconfig(self._led, fill=col)

    def _log_gesture(self, msg):
        text = f"[{time.strftime('%H:%M:%S')}]  {msg.get('gesture','?'):<12}{msg.get('axis','')}\n"
        self.gesture_log.configure(state="normal")
        self.gesture_log.insert("end", text)
        self.gesture_log.see("end")
        lines = int(self.gesture_log.index("end-1c").split(".")[0])
        if lines > 80: self.gesture_log.delete("1.0", f"{lines-80}.0")
        self.gesture_log.configure(state="disabled")

    def _clear_log(self):
        self.gesture_log.configure(state="normal")
        self.gesture_log.delete("1.0", "end")
        self.gesture_log.configure(state="disabled")

    # ── Actions ───────────────────────────────────────────────────────────────
    def _scan_ble(self):
        self._set_status("scanning")
        def run_scan():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            devices = loop.run_until_complete(BleakScanner.discover(timeout=3.0))
            self.after(0, lambda: self._update_device_list(devices))
        threading.Thread(target=run_scan, daemon=True).start()

    def _update_device_list(self, devices):
        names = []
        self.device_addresses = {}
        for d in devices:
            name = d.name or "Unknown"
            display = f"{name} ({d.address})"
            names.append(display)
            self.device_addresses[display] = d.address
        self.device_cb["values"] = names
        if names:
            self.device_var.set(names[0])
        self._set_status("disconnected")

    def _connect(self):
        display = self.device_var.get()
        if not display or display not in self.device_addresses:
            messagebox.showwarning("No device", "Scan and select a BLE device first.")
            return
        address = self.device_addresses[display]
        if self.ble_thread: self.ble_thread.stop()
        self._set_status("connecting")
        self.ble_thread = BleConfigThread(address, self._on_data, self._on_gesture, self._on_status)
        self.ble_thread.start()

    def _apply(self):
        self._widgets_to_cfg()
        if self.ble_thread and self.ble_thread.connected:
            self.ble_thread.send({"cfg": self.cfg})
        else:
            messagebox.showinfo("Not connected", "Connect to ESP32 first.")

    def _save_config(self):
        self._widgets_to_cfg()
        with open(SAVE_FILE, "w") as f: json.dump(self.cfg, f, indent=2)
        if self.ble_thread and self.ble_thread.connected:
            self.ble_thread.send({"save": 1})
        self._set_status("ack")

    def _load_config(self):
        try:
            with open(SAVE_FILE) as f: self.cfg.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError): pass
        if "tiltThreshDeg" not in self.cfg and "clickThreshDeg" in self.cfg:
            self.cfg["tiltThreshDeg"] = self.cfg["clickThreshDeg"]
        self.cfg.pop("clickThreshDeg", None)
        # remove normalization of shortcuts since it was undefined anyway

    def _load_and_apply(self):
        self._load_config(); self._apply_cfg_to_widgets(); self._apply()

    def _reset_defaults(self):
        self.cfg = dict(DEFAULT_CFG); self._apply_cfg_to_widgets(); self._apply()

    def _on_close(self):
        if self.ble_thread: self.ble_thread.stop()
        self.destroy()

if __name__ == "__main__":
    App().mainloop()