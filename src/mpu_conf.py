#!/usr/bin/env python3
"""
ESP32 MPU Mouse — Axis Calibration GUI
Requirements: pip install pyserial
"""

import json, pathlib, threading, time
import tkinter as tk
from tkinter import ttk, messagebox
import serial, serial.tools.list_ports

# ─────────────────────────────────────────────────────────────────────────────
AXIS_NAMES  = ["Yaw (0)", "Pitch (1)", "Roll (2)"]
BASE_DIR = pathlib.Path(__file__).resolve().parent
SAVE_FILE   = BASE_DIR / "mpu_config.json"
OVERLAY_FILE = BASE_DIR / "overlay_config.json"

OVERLAY_KEYS = {
    "scrollSpeed", "overlayIconRadius", "dwellTime",
    "rcIconX", "rcIconY", "dragIconX", "dragIconY",
    "kbIconX", "kbIconY", "copyIconX", "copyIconY",
    "pasteIconX", "pasteIconY",
    "mainBtnX", "mainBtnY", "scrollUpX", "scrollUpY",
    "scrollDownX", "scrollDownY"
}

DEFAULT_CFG = {
    "cursorXAxis": 0, "cursorYAxis": 2, "clickAxis": 1,
    "invertX": False, "invertY": False, "invertClick": False,
    "deadzoneX": 1.5, "deadzoneY": 1.5, "deadzoneClick": 2.0,
    "gainX": 0.3, "gainY": 0.3,
    "clickThreshDeg": 30.0,
    "flickVelThresh": 120.0, "flickReturnDeg": 8.0, "flickConfirmMs": 300,
    "shakeVelThresh": 60.0, "doubleTiltDeg": 25.0, "circleMinSpeed": 20.0,
    "enableFlick": True, "enableShake": True,
    "enableDoubleTilt": True, "enableCircle": True,
    # Overlay settings
    "scrollSpeed": 2,         # overlay scroll initial speed (1–10)
    "overlayIconRadius": 19,  # overlay action-button radius px (10–40)
    "dwellTime": 0.75,        # seconds to hover before action triggers
    "rcIconX": -1, "rcIconY": -1,
    "dragIconX": -1, "dragIconY": -1,
    "kbIconX": -1, "kbIconY": -1,
    "copyIconX": -1, "copyIconY": -1,
    "pasteIconX": -1, "pasteIconY": -1,
    "mainBtnX": -1, "mainBtnY": -1,
    "scrollUpX": -1, "scrollUpY": -1,
    "scrollDownX": -1, "scrollDownY": -1,
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


# ─────────────────────────────────────────────────────────────────────────────
class SerialThread(threading.Thread):
    def __init__(self, port, baud, on_data, on_gesture, on_status):
        super().__init__(daemon=True)
        self.port, self.baud = port, baud
        self.on_data, self.on_gesture, self.on_status = on_data, on_gesture, on_status
        self._stop = threading.Event()
        self.ser = None
        self.connected = False

    def run(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
            time.sleep(1.5)
            self.ser.write(b'{"ping":1}\n')
            deadline = time.time() + 3
            while time.time() < deadline:
                line = self.ser.readline().decode(errors="ignore").strip()
                if line == '{"pong":1}':
                    self.connected = True
                    self.on_status("connected")
                    break
            else:
                self.on_status("no_response"); return
            while not self._stop.is_set():
                raw = self.ser.readline().decode(errors="ignore").strip()
                if not raw: continue
                try:
                    msg = json.loads(raw)
                    if "a" in msg:         self.on_data(msg["a"])
                    elif "gesture" in msg: self.on_gesture(msg)
                    elif "ack" in msg:     self.on_status("ack")
                except json.JSONDecodeError:
                    pass
        except serial.SerialException as e:
            self.on_status(f"error:{e}")
        finally:
            if self.ser and self.ser.is_open: self.ser.close()
            self.connected = False

    def send(self, payload):
        if self.ser and self.ser.is_open:
            self.ser.write((json.dumps(payload) + "\n").encode())

    def stop(self):
        self._stop.set()


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
        self._saved_cfg = dict(self.cfg)
        self.serial = None
        self.axes   = [0.0, 0.0, 0.0]

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
        # Notebook tab styling — dark theme
        s.configure("TNotebook", background=C["bg0"], borderwidth=0)
        s.configure("TNotebook.Tab",
                    background=C["bg2"], foreground=C["text1"],
                    padding=(14, 6), font=("Consolas", 9, "bold"),
                    borderwidth=0)
        s.map("TNotebook.Tab",
              background=[("selected", C["bg1"])],
              foreground=[("selected", C["accent"])],
              expand=[("selected", [0, 2, 0, 0])])

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        layout = tk.Frame(self, bg=C["bg0"])
        layout.pack(fill="both", expand=True)
        layout.columnconfigure(0, weight=1)
        layout.rowconfigure(1, weight=1)

        # Top bar
        top = tk.Frame(layout, bg=C["bg0"],
                       highlightthickness=1, highlightbackground=C["border"])
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(4, weight=1)

        tk.Label(top, text="MPU MOUSE  CONFIG",
                 bg=C["bg0"], fg=C["accent"],
                 font=("Consolas", 11, "bold"), padx=14, pady=8).grid(row=0, column=0)
        tk.Frame(top, bg=C["border"], width=1).grid(row=0, column=1, sticky="ns", pady=4)

        tk.Label(top, text="PORT", bg=C["bg0"], fg=C["text1"],
                 font=FONT_TINY, padx=8).grid(row=0, column=2)
        self.port_var = tk.StringVar()
        self.port_cb  = ttk.Combobox(top, textvariable=self.port_var,
                                     width=13, state="readonly")
        self.port_cb.grid(row=0, column=3, padx=(0, 4))
        self._btn(top, "⟳", self._refresh_ports).grid(row=0, column=4, sticky="w", padx=2)
        self._btn(top, "CONNECT", self._connect, accent=True).grid(
            row=0, column=5, padx=6)

        self.led = tk.Canvas(top, width=10, height=10, bg=C["bg0"], highlightthickness=0)
        self.led.grid(row=0, column=6, padx=(12, 3))
        self._led = self.led.create_oval(1, 1, 9, 9, fill=C["red"], outline="")

        self.status_lbl = tk.Label(top, text="DISCONNECTED", bg=C["bg0"],
                                   fg=C["red"], font=("Consolas", 8, "bold"), padx=8)
        self.status_lbl.grid(row=0, column=7, sticky="e")
        self._refresh_ports()

        # Bottom action bar pinned to the last grid row.
        bot = tk.Frame(layout, bg=C["bg0"],
                       highlightthickness=1, highlightbackground=C["border"])
        bot.grid(row=2, column=0, sticky="ew")
        inner = tk.Frame(bot, bg=C["bg0"])
        inner.pack(side="right", padx=10, pady=6)
        for text, cmd, acc in [
            ("APPLY",           self._apply,         True),
            ("SAVE",            self._save_config,   False),
            ("RESET",           self._reset_defaults,False),
            ("CLOSE",           self._on_close,      False),
        ]:
            self._btn(inner, text, cmd, accent=acc).pack(side="left", padx=3)

        # ── Notebook (tabs) ───────────────────────────────────────────────
        self.notebook = ttk.Notebook(layout)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

        # ── Tab 1: MPU Config (existing layout, unchanged) ────────────────
        mpu_tab = tk.Frame(self.notebook, bg=C["bg0"])
        self.notebook.add(mpu_tab, text="  MPU Config  ")

        mpu_tab.columnconfigure(0, weight=3, minsize=520)
        mpu_tab.columnconfigure(1, weight=2, minsize=300)
        mpu_tab.rowconfigure(0, weight=1)

        left  = tk.Frame(mpu_tab, bg=C["bg0"])
        right = tk.Frame(mpu_tab, bg=C["bg0"])
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

        # ── Tab 2: Overlay Config ─────────────────────────────────────────
        overlay_tab = tk.Frame(self.notebook, bg=C["bg0"])
        self.notebook.add(overlay_tab, text="  Overlay Config  ")
        self._build_overlay_tab(overlay_tab)

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
            self.dead_vizzes[key] = v          # register BEFORE slider
            sl = PrecisionSlider(cell, 0.0, 20.0, 0.1,
                                 initial=self.cfg.get(key, 1.5), width=70,
                                 on_change=lambda val, k=key: self._on_dead(k, val))
            sl.pack(pady=(3,0))
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
            ("clickThreshDeg","Click angle",    "°",    5,  90,  0.5),
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

    def _build_overlay_tab(self, parent):
        """Build the Overlay Configuration tab contents."""
        import math as _math
        self._math = _math
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=0)

        # Detect actual screen resolution for accurate coordinate mapping
        self._SCREEN_W = self.winfo_screenwidth()
        self._SCREEN_H = self.winfo_screenheight()

        self.overlay_sliders = {}

        # Left column
        left_col = tk.Frame(parent, bg=C["bg0"])
        left_col.grid(row=0, column=0, sticky="nsew", padx=(10,5), pady=(10,5))
        left_col.rowconfigure(0, weight=0)
        left_col.rowconfigure(1, weight=1)
        left_col.columnconfigure(0, weight=1)

        card_icons = Card(left_col, "OVERLAY ICON SIZE")
        card_icons.grid(row=0, column=0, sticky="ew", pady=(0,5))
        p = card_icons.body

        row_f = tk.Frame(p, bg=C["bg1"])
        row_f.pack(fill="x", pady=4)
        tk.Label(row_f, text="Icon radius (px)", bg=C["bg1"], fg=C["text1"],
                 font=FONT_TINY, width=18, anchor="w").pack(side="left")
        sl = PrecisionSlider(row_f, 10, 40, 1,
                             initial=self.cfg.get("overlayIconRadius", 19),
                             width=160,
                             on_change=self._on_icon_radius_change)
        sl.pack(side="left", padx=4)
        self.overlay_sliders["overlayIconRadius"] = sl

        preview_frame = tk.Frame(p, bg=C["bg2"],
                                 highlightthickness=1,
                                 highlightbackground=C["border"])
        preview_frame.pack(fill="x", pady=(8,4))
        self._icon_preview = tk.Canvas(preview_frame, width=220, height=130,
                                       bg=C["bg2"], highlightthickness=0)
        self._icon_preview.pack()
        self._preview_anim_r   = float(self.cfg.get("overlayIconRadius", 19))
        self._preview_target_r = self._preview_anim_r
        self._preview_anim_id  = None
        self._draw_icon_preview()

        card_dwell = Card(left_col, "DWELL TIME")
        card_dwell.grid(row=1, column=0, sticky="new", pady=(5,0))
        pd = card_dwell.body
        tk.Label(pd, text="How long the user must hover\\n"
                          "over an icon before it triggers.",
                 bg=C["bg1"], fg=C["text2"], font=FONT_TINY,
                 justify="left").pack(anchor="w", pady=(0,10))
        row_f = tk.Frame(pd, bg=C["bg1"])
        row_f.pack(fill="x", pady=4)
        tk.Label(row_f, text="Dwell time (s)", bg=C["bg1"], fg=C["text1"],
                 font=FONT_TINY, width=18, anchor="w").pack(side="left")
        sl = PrecisionSlider(row_f, 0.3, 5.0, 0.05,
                             initial=self.cfg.get("dwellTime", 0.75), width=160)
        sl.pack(side="left", padx=4)
        self.overlay_sliders["dwellTime"] = sl

        # Right column
        right_col = tk.Frame(parent, bg=C["bg0"])
        right_col.grid(row=0, column=1, sticky="nsew", padx=(5,10), pady=(10,5))
        right_col.rowconfigure(0, weight=0)
        right_col.rowconfigure(1, weight=1)
        right_col.columnconfigure(0, weight=1)

        card_scroll = Card(right_col, "OVERLAY SCROLL")
        card_scroll.grid(row=0, column=0, sticky="ew", pady=(0,5))
        p2 = card_scroll.body
        row_f = tk.Frame(p2, bg=C["bg1"])
        row_f.pack(fill="x", pady=4)
        tk.Label(row_f, text="Scroll speed (ticks)", bg=C["bg1"], fg=C["text1"],
                 font=FONT_TINY, width=18, anchor="w").pack(side="left")
        sl = PrecisionSlider(row_f, 1, 10, 1,
                             initial=self.cfg.get("scrollSpeed", 2), width=160)
        sl.pack(side="left", padx=4)
        self.overlay_sliders["scrollSpeed"] = sl

        card_pos = Card(right_col, "ICON POSITIONS  (drag to place)")
        card_pos.grid(row=1, column=0, sticky="nsew", pady=(5,0))
        pp = card_pos.body
        pp.rowconfigure(1, weight=1)
        pp.columnconfigure(0, weight=1)
        tk.Label(pp, text="Drag icons directly on the mini-screen.\\n"
                  "Coordinates map to your real display resolution.",
                 bg=C["bg1"], fg=C["text2"], font=FONT_TINY,
                 justify="left").pack(anchor="w", pady=(0,4))
        # Resolution override row
        res_row = tk.Frame(pp, bg=C["bg1"])
        res_row.pack(fill="x", pady=(0, 4))
        tk.Label(res_row, text="Screen W:", bg=C["bg1"], fg=C["text1"],
                 font=FONT_TINY).pack(side="left")
        self._res_w_var = tk.IntVar(value=self._SCREEN_W)
        self._res_w_spinbox = tk.Spinbox(res_row, from_=640, to=7680, increment=1,
                   textvariable=self._res_w_var, width=6,
                   bg=C["bg3"], fg=C["text0"], relief="flat",
                   font=FONT_MONO, insertbackground=C["accent"],
                   buttonbackground=C["bg2"],
                   command=self._on_res_change)
        self._res_w_spinbox.pack(side="left", padx=(2,8))
        self._res_w_spinbox.bind("<Return>", lambda e: self._on_res_change())
        self._res_w_spinbox.bind("<FocusOut>", lambda e: self._on_res_change())
        tk.Label(res_row, text="H:", bg=C["bg1"], fg=C["text1"],
                 font=FONT_TINY).pack(side="left")
        self._res_h_var = tk.IntVar(value=self._SCREEN_H)
        self._res_h_spinbox = tk.Spinbox(res_row, from_=480, to=4320, increment=1,
                   textvariable=self._res_h_var, width=6,
                   bg=C["bg3"], fg=C["text0"], relief="flat",
                   font=FONT_MONO, insertbackground=C["accent"],
                   buttonbackground=C["bg2"],
                   command=self._on_res_change)
        self._res_h_spinbox.pack(side="left", padx=(2,8))
        self._res_h_spinbox.bind("<Return>", lambda e: self._on_res_change())
        self._res_h_spinbox.bind("<FocusOut>", lambda e: self._on_res_change())

        self._pos_canvas = tk.Canvas(pp, bg="#0a0c10",
                                     highlightthickness=1,
                                     highlightbackground=C["border"])
        self._pos_canvas.pack(fill="both", expand=True, pady=(4,4))

        self._drag_icon = None
        self._drag_offset = (0, 0)
        self._icon_dots = {}
        self._pos_canvas.bind("<Configure>", lambda e: self._redraw_pos_canvas())
        self._pos_canvas.bind("<Button-1>",  self._on_pos_drag_start)
        self._pos_canvas.bind("<B1-Motion>", self._on_pos_drag_move)
        self._pos_canvas.bind("<ButtonRelease-1>", self._on_pos_drag_end)

    def _on_icon_radius_change(self, val):
        if not hasattr(self, '_preview_target_r'): return
        self._preview_target_r = float(val)
        if self._preview_anim_id is None:
            self._preview_anim_step()

    def _preview_anim_step(self):
        diff = self._preview_target_r - self._preview_anim_r
        if abs(diff) < 0.3:
            self._preview_anim_r = self._preview_target_r
            self._draw_icon_preview()
            self._preview_anim_id = None
            return
        self._preview_anim_r += diff * 0.25
        self._draw_icon_preview()
        self._preview_anim_id = self.after(16, self._preview_anim_step)

    def _draw_icon_preview(self):
        if not hasattr(self, '_icon_preview'): return
        c = self._icon_preview
        c.delete("all")
        r  = self._preview_anim_r
        cx, cy = 110, 65
        _m = self._math

        for dr, a in [(12, 15), (7, 28)]:
            gr = r + dr
            g_val = int(212 * a / 255)
            b_val = int(255 * a / 255)
            col = f"#00{g_val:02x}{b_val:02x}"
            c.create_oval(cx-gr, cy-gr, cx+gr, cy+gr, fill="", outline=col, width=1)

        c.create_oval(cx-r, cy-r, cx+r, cy+r, fill="#1e2636", outline="", width=0)
        inner = r * 0.7
        c.create_oval(cx-inner, cy-inner, cx+inner, cy+inner, fill="#222d40", outline="", width=0)

        ring_r = r + 4
        prog   = 0.70
        steps  = max(4, int(prog * 36))
        for s in range(steps):
            a1 = _m.radians(90 - s * (360 * prog / steps))
            a2 = _m.radians(90 - (s+1) * (360 * prog / steps))
            x1 = cx + ring_r * _m.cos(a1)
            y1 = cy - ring_r * _m.sin(a1)
            x2 = cx + ring_r * _m.cos(a2)
            y2 = cy - ring_r * _m.sin(a2)
            frac = s / max(steps, 1)
            gv = int(180 + 75 * frac)
            bv = int(255 - 40 * frac)
            col = f"#00{gv:02x}{bv:02x}"
            c.create_line(x1, y1, x2, y2, fill=col, width=2)

        c.create_oval(cx-r, cy-r, cx+r, cy+r, fill="", outline=C["accent"], width=1.5)

        fs = max(8, int(r * 0.45))
        c.create_text(cx, cy - 2, text="[C]", fill=C["text0"], font=("Consolas", fs, "bold"))
        c.create_text(cx, cy + r + 14, text=f"r = {int(round(self._preview_target_r))} px",
                      fill=C["text1"], font=FONT_TINY)

    def _on_res_change(self):
        """Called when the user edits the resolution spinboxes."""
        try:
            w = int(self._res_w_var.get())
            h = int(self._res_h_var.get())
            if w >= 640 and h >= 480:
                self._SCREEN_W = w
                self._SCREEN_H = h
                self._redraw_pos_canvas()
        except (tk.TclError, ValueError):
            pass

    def _map_to_canvas(self, ix, iy, sx, sy, sw, sh):
        px = sx + (ix / self._SCREEN_W) * sw
        py = sy + (iy / self._SCREEN_H) * sh
        return int(px), int(py)

    def _map_from_canvas(self, cx, cy, sx, sy, sw, sh):
        """Convert canvas pixel position to screen coordinates, clamped to [0, screen size]."""
        lx = max(0, min(sw, cx - sx))
        ly = max(0, min(sh, cy - sy))
        ix = round((lx / sw) * self._SCREEN_W)
        iy = round((ly / sh) * self._SCREEN_H)
        return int(ix), int(iy)

    def _redraw_pos_canvas(self):
        if not hasattr(self, '_pos_canvas'): return
        c = self._pos_canvas
        c.delete("all")
        cw, ch = c.winfo_width(), c.winfo_height()
        if cw < 20 or ch < 20: return
        m = 8
        sx, sy, sw, sh = m, m, cw - m*2, ch - m*2
        c.create_rectangle(sx, sy, sx+sw, sy+sh, outline=C["border2"], fill="#0e1018", width=1)

        # Draw mini-screen grid for easier positioning.
        vx1 = sx + int(sw * 0.25)
        vx2 = sx + int(sw * 0.50)
        vx3 = sx + int(sw * 0.75)
        hy1 = sy + int(sh * 0.25)
        hy2 = sy + int(sh * 0.50)
        hy3 = sy + int(sh * 0.75)
        for x in (vx1, vx2, vx3):
            c.create_line(x, sy, x, sy + sh, fill=C["border"], dash=(2, 3))
        for y in (hy1, hy2, hy3):
            c.create_line(sx, y, sx + sw, y, fill=C["border"], dash=(2, 3))

        c.create_text(
            sx + 8, sy + 8,
            text=f"{self._SCREEN_W} x {self._SCREEN_H}",
            fill=C["text2"], font=FONT_TINY, anchor="nw"
        )
        
        self._icon_dots = {}
        icons_to_draw = [
            ("rc",    C["amber"],  "rcIconX",   "rcIconY",   "[⧉] DblClk"),
            ("drag",  C["purple"], "dragIconX", "dragIconY", "[↕] Drag"),
            ("kb",    "#ff00ff",   "kbIconX",   "kbIconY",   "[⌨] KB"),
            ("copy",  C["accent"], "copyIconX", "copyIconY", "[📋] Copy"),
            ("paste", C["green"],  "pasteIconX","pasteIconY","[📌] Paste"),
            ("main",  "#ffffff",   "mainBtnX",  "mainBtnY",  "[●] Main"),
            ("up",    C["accent2"],"scrollUpX", "scrollUpY", "[▲] Up"),
            ("down",  C["accent2"],"scrollDownX","scrollDownY","[▼] Down")
        ]
        
        DEFAULT_OVERLAY_POSITIONS = {
            "rc": (300, 300),
            "drag": (380, 300),
            "kb": (460, 300),
            "copy": (540, 300),
            "paste": (620, 300),
            "main": (380, 380),
            "up": (300, 460),
            "down": (460, 460)
        }
        
        for name, color, xk, yk, lbl in icons_to_draw:
            ix = self.cfg.get(xk)
            iy = self.cfg.get(yk)
            
            if ix is None or ix == -1:
                ix = DEFAULT_OVERLAY_POSITIONS.get(name, (20, 20))[0]
            if iy is None or iy == -1:
                iy = DEFAULT_OVERLAY_POSITIONS.get(name, (20, 20))[1]
                
            ix = max(0, min(self._SCREEN_W, ix))
            iy = max(0, min(self._SCREEN_H, iy))

            px, py = self._map_to_canvas(ix, iy, sx, sy, sw, sh)
            r = 8
            dot = c.create_oval(px-r, py-r, px+r, py+r, fill=color, outline="#ffffff", width=1, tags=(name,))
            c.create_text(px+14, py, text=f"{lbl} ({ix},{iy})", fill=C["text1"], font=("Consolas", 7), anchor="w", tags=(f"{name}_lbl",))
            self._icon_dots[name] = dot

    def _on_pos_drag_start(self, event):
        c = self._pos_canvas
        for item in c.find_overlapping(event.x-12, event.y-12, event.x+12, event.y+12):
            tags = c.gettags(item)
            for name in ["rc", "drag", "kb", "copy", "paste", "main", "up", "down"]:
                if name in tags:
                    self._drag_icon = name
                    x1, y1, x2, y2 = c.coords(item)
                    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                    self._drag_offset = (event.x - cx, event.y - cy)
                    return
        self._drag_icon = None

    def _on_pos_drag_move(self, event):
        if not self._drag_icon: return
        c = self._pos_canvas
        m = 8
        cw, ch = c.winfo_width(), c.winfo_height()
        sx, sy, sw, sh = m, m, cw - m*2, ch - m*2

        ox, oy = self._drag_offset
        nx = max(sx, min(sx + sw, event.x - ox))
        ny = max(sy, min(sy + sh, event.y - oy))
        dot = self._icon_dots.get(self._drag_icon)
        if dot:
            c.coords(dot, nx-8, ny-8, nx+8, ny+8)
            lbl = c.find_withtag(f"{self._drag_icon}_lbl")
            if lbl:
                scr_x, scr_y = self._map_from_canvas(nx, ny, sx, sy, sw, sh)
                label_text = c.itemcget(lbl[0], "text")
                if " (" in label_text:
                    base = label_text.split(" (", 1)[0]
                else:
                    base = label_text
                c.coords(lbl[0], nx+14, ny)
                c.itemconfigure(lbl[0], text=f"{base} ({scr_x},{scr_y})")

            if self._drag_icon == "rc":
                self.cfg["rcIconX"], self.cfg["rcIconY"] = self._map_from_canvas(nx, ny, sx, sy, sw, sh)
            elif self._drag_icon == "drag":
                self.cfg["dragIconX"], self.cfg["dragIconY"] = self._map_from_canvas(nx, ny, sx, sy, sw, sh)
            elif self._drag_icon == "kb":
                self.cfg["kbIconX"], self.cfg["kbIconY"] = self._map_from_canvas(nx, ny, sx, sy, sw, sh)
            elif self._drag_icon == "copy":
                self.cfg["copyIconX"], self.cfg["copyIconY"] = self._map_from_canvas(nx, ny, sx, sy, sw, sh)
            elif self._drag_icon == "paste":
                self.cfg["pasteIconX"], self.cfg["pasteIconY"] = self._map_from_canvas(nx, ny, sx, sy, sw, sh)
            elif self._drag_icon == "main":
                self.cfg["mainBtnX"], self.cfg["mainBtnY"] = self._map_from_canvas(nx, ny, sx, sy, sw, sh)
            elif self._drag_icon == "up":
                self.cfg["scrollUpX"], self.cfg["scrollUpY"] = self._map_from_canvas(nx, ny, sx, sy, sw, sh)
            elif self._drag_icon == "down":
                self.cfg["scrollDownX"], self.cfg["scrollDownY"] = self._map_from_canvas(nx, ny, sx, sy, sw, sh)

    def _on_pos_drag_end(self, event):
        if not self._drag_icon: return
        c = self._pos_canvas
        m = 8
        cw, ch = c.winfo_width(), c.winfo_height()
        sx, sy, sw, sh = m, m, cw - m*2, ch - m*2

        # Use offset-corrected position (same as _on_pos_drag_move) so the
        # final coordinate matches exactly where the dot was left, not where
        # the raw mouse pointer is (which differs by the drag offset).
        ox, oy = self._drag_offset
        nx = max(sx, min(sx + sw, event.x - ox))
        ny = max(sy, min(sy + sh, event.y - oy))
        scr_x, scr_y = self._map_from_canvas(nx, ny, sx, sy, sw, sh)

        if self._drag_icon == "rc":
            self.cfg["rcIconX"], self.cfg["rcIconY"] = scr_x, scr_y
        elif self._drag_icon == "drag":
            self.cfg["dragIconX"], self.cfg["dragIconY"] = scr_x, scr_y
        elif self._drag_icon == "kb":
            self.cfg["kbIconX"], self.cfg["kbIconY"] = scr_x, scr_y
        elif self._drag_icon == "copy":
            self.cfg["copyIconX"], self.cfg["copyIconY"] = scr_x, scr_y
        elif self._drag_icon == "paste":
            self.cfg["pasteIconX"], self.cfg["pasteIconY"] = scr_x, scr_y
        elif self._drag_icon == "main":
            self.cfg["mainBtnX"], self.cfg["mainBtnY"] = scr_x, scr_y
        elif self._drag_icon == "up":
            self.cfg["scrollUpX"], self.cfg["scrollUpY"] = scr_x, scr_y
        elif self._drag_icon == "down":
            self.cfg["scrollDownX"], self.cfg["scrollDownY"] = scr_x, scr_y

        self._drag_icon = None
        self._drag_offset = (0, 0)
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
        if key in self.dead_vizzes:
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
        for k, sl in self.overlay_sliders.items():
            sl.set(self.cfg.get(k, sl.get()))
        # redraw canvas to reflect reset config
        self.after(50, lambda: hasattr(self, '_pos_canvas') and self._redraw_pos_canvas())

    def _widgets_to_cfg(self):
        for k, v in self.axis_vars.items():
            try: self.cfg[k] = AXIS_NAMES.index(v.get())
            except ValueError: pass
        for k, v in self.invert_vars.items():  self.cfg[k] = v.get()
        for k, v in self.gesture_toggle_vars.items(): self.cfg[k] = v.get()
        for k, sl in self.dead_sliders.items(): self.cfg[k] = sl.get()
        for k, sl in self.gain_sliders.items(): self.cfg[k] = sl.get()
        for k, sl in self.thresh_sliders.items(): self.cfg[k] = sl.get()
        for k, sl in self.overlay_sliders.items(): self.cfg[k] = sl.get()

    # ── Serial callbacks ──────────────────────────────────────────────────────
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
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_cb["values"] = ports
        if ports and not self.port_var.get(): self.port_var.set(ports[0])

    def _connect(self):
        port = self.port_var.get()
        if not port: messagebox.showwarning("No port", "Select a serial port first."); return
        if self.serial: self.serial.stop()
        self._set_status("connecting")
        self.serial = SerialThread(port, 115200,
                                   self._on_data, self._on_gesture, self._on_status)
        self.serial.start()

    def _apply(self):
        self._widgets_to_cfg()
        ov_out = {k: v for k, v in self.cfg.items() if k in OVERLAY_KEYS}
        try:
            with open(OVERLAY_FILE, "w") as f:
                json.dump(ov_out, f, indent=2)
        except OSError:
            pass
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(json.dumps(ov_out).encode('utf-8'), ("127.0.0.1", 55555))
        except Exception:
            pass
        if self.serial and self.serial.connected:
            mpu_out = {k: v for k, v in self.cfg.items() if k not in OVERLAY_KEYS}
            self.serial.send({"cfg": mpu_out})

    def _save_config(self):
        self._widgets_to_cfg()
        self._saved_cfg = dict(self.cfg)
        mpu_out = {k: v for k, v in self.cfg.items() if k not in OVERLAY_KEYS}
        ov_out  = {k: v for k, v in self.cfg.items() if k in OVERLAY_KEYS}
        with open(SAVE_FILE, "w") as f: json.dump(mpu_out, f, indent=2)
        with open(OVERLAY_FILE, "w") as f: json.dump(ov_out, f, indent=2)
        self._set_status("ack")

    def _load_config(self):
        try:
            with open(SAVE_FILE) as f: self.cfg.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError): pass
        try:
            with open(OVERLAY_FILE) as f: self.cfg.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError): pass
        self._saved_cfg = dict(self.cfg)

    def _load_and_apply(self):
        self._load_config(); self._apply_cfg_to_widgets(); self._apply()

    def _reset_defaults(self):
        self.cfg = dict(DEFAULT_CFG)
        self._apply_cfg_to_widgets()
        self._apply()

    def _on_close(self):
        if self.serial: self.serial.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()