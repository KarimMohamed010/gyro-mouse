#!/usr/bin/env python3
"""
ESP32 MPU Mouse — Configuration GUI
Communicates exclusively via USB HID feature reports (hidapi).
No BLE, no serial dependency.

Requirements: pip install hid
"""

import json
import threading
import time
import tkinter as tk
import ctypes
from ctypes import wintypes
from tkinter import ttk, messagebox

try:
    import hid
except ImportError as e:
    hid = None
    HID_IMPORT_ERROR = str(e)
else:
    HID_IMPORT_ERROR = None


def hid_import_diagnostic():
    if not HID_IMPORT_ERROR:
        return "hid is not available in this Python environment.\nRun: pip install hid"
    return (
        "Python found the hid package, but failed to load its native HID library.\n"
        "On Windows this usually means hidapi.dll (or libhidapi-0.dll) is missing from PATH.\n\n"
        f"Import error:\n{HID_IMPORT_ERROR}\n\n"
        "Fix options:\n"
        "1) Use Python 3.12 and reinstall: pip install --force-reinstall hid\n"
        "2) Install the HIDAPI runtime DLL and ensure it is on PATH"
    )

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
AXIS_NAMES = ["Yaw (0)", "Pitch (1)", "Roll (2)"]
SAVE_FILE  = "mpu_config.json"

DEFAULT_CFG = {
    "cursorXAxis": 0, "cursorYAxis": 2, "clickAxis": 1,
    "invertX": False, "invertY": False, "invertClick": False,
    "deadzoneX": 1.5, "deadzoneY": 1.5, "deadzoneClick": 2.0,
    "gainX": 0.3, "gainY": 0.3,
    "tiltThreshDeg": 30.0,
    "flickVelThresh": 120.0, "flickReturnDeg": 8.0, "flickConfirmMs": 300,
    "shakeVelThresh": 60.0, "doubleTiltDeg": 25.0, "circleMinSpeed": 20.0,
    "enableFlick": True, "enableShake": True,
    "enableDoubleTilt": True, "enableCircle": True, "enableClicks": True,
}

HID_VID     = 0xE502
HID_PID     = 0xA111
HID_PRODUCT = "ESP32 MPU Mouse"

REPORT_ID_GESTURE = 2
REPORT_ID_FEATURE = 3

FEATURE_PAGE_BASIC           = 0
FEATURE_PAGE_GAINS           = 1
FEATURE_PAGE_FLICK           = 2
FEATURE_PAGE_OTHER_GESTURES  = 3
FEATURE_PAGE_SELECT          = 0x7F

AUTO_CONNECT_POLL_MS = 1500
AUTO_CALIBRATION_MS = 2500

GESTURE_NAMES = {1: "Flick", 2: "Shake", 3: "DoubleTilt", 4: "Circle"}
AXIS_SHORT    = ["YAW", "PITCH", "ROLL", ""]

# Global hotkey for RECENTER (Windows only)
HOTKEY_MOD_CONTROL = 0x0002
HOTKEY_MOD_ALT = 0x0001
HOTKEY_VK_Q = 0x51
HOTKEY_LABEL = "Ctrl+Alt+Q"
HOTKEY_MODIFIERS = HOTKEY_MOD_CONTROL | HOTKEY_MOD_ALT

# ─── Colour palette ───────────────────────────────────────────────────────────
C = {
    "bg0":    "#0d0f12", "bg1": "#13161b", "bg2": "#1a1e25", "bg3": "#222733",
    "border": "#2a3040", "border2": "#3a4558",
    "accent": "#00d4ff", "accent2": "#0099bb",
    "green":  "#00e676", "amber": "#ffab40", "red": "#ff5252", "purple": "#b388ff",
    "text0":  "#e8ecf4", "text1": "#8892a4", "text2": "#4a5568",
}
FONT_MONO = ("Consolas", 9)
FONT_TINY = ("Consolas", 8)
FONT_HEAD = ("Segoe UI", 9, "bold")


# ─────────────────────────────────────────────────────────────────────────────
# Feature report encode / decode  (must match firmware exactly)
# ─────────────────────────────────────────────────────────────────────────────
def _u8(value, scale=10.0):
    return max(0, min(255, int(round(float(value) * scale))))

def _u16(value, scale=1.0):
    return max(0, min(65535, int(round(float(value) * scale))))

def _put16(buf, idx, v):
    v = int(v) & 0xFFFF
    buf[idx]     = v & 0xFF
    buf[idx + 1] = (v >> 8) & 0xFF

def _get16(buf, idx):
    return int(buf[idx]) | (int(buf[idx + 1]) << 8)


def cfg_to_feature_pages(cfg):
    pages = []

    p = [0] * 8
    p[0] = FEATURE_PAGE_BASIC
    p[1] = ((int(cfg.get("cursorXAxis", 0)) & 0x03)
            | ((int(cfg.get("cursorYAxis", 2)) & 0x03) << 2)
            | ((int(cfg.get("clickAxis",   1)) & 0x03) << 4))
    p[2] = ((0x01 if cfg.get("invertX",        False) else 0)
            | (0x02 if cfg.get("invertY",       False) else 0)
            | (0x04 if cfg.get("invertClick",   False) else 0)
            | (0x08 if cfg.get("enableClicks",  True)  else 0)
            | (0x10 if cfg.get("enableFlick",   True)  else 0)
            | (0x20 if cfg.get("enableShake",   True)  else 0)
            | (0x40 if cfg.get("enableDoubleTilt", True) else 0)
            | (0x80 if cfg.get("enableCircle",  True)  else 0))
    p[3] = _u8(cfg.get("deadzoneX",     1.5))
    p[4] = _u8(cfg.get("deadzoneY",     1.5))
    p[5] = _u8(cfg.get("deadzoneClick", 2.0))
    p[6] = _u8(cfg.get("tiltThreshDeg", 30.0))
    pages.append(p)

    p = [0] * 8
    p[0] = FEATURE_PAGE_GAINS
    _put16(p, 1, _u16(cfg.get("gainX", 0.3), 100.0))
    _put16(p, 3, _u16(cfg.get("gainY", 0.3), 100.0))
    pages.append(p)

    p = [0] * 8
    p[0] = FEATURE_PAGE_FLICK
    _put16(p, 1, _u16(cfg.get("flickVelThresh", 120.0)))
    p[3] = _u8(cfg.get("flickReturnDeg", 8.0))
    _put16(p, 4, _u16(cfg.get("flickConfirmMs", 300.0)))
    pages.append(p)

    p = [0] * 8
    p[0] = FEATURE_PAGE_OTHER_GESTURES
    _put16(p, 1, _u16(cfg.get("shakeVelThresh", 60.0)))
    p[3] = _u8(cfg.get("doubleTiltDeg",  25.0))
    _put16(p, 4, _u16(cfg.get("circleMinSpeed", 20.0)))
    pages.append(p)

    return pages


def merge_feature_page(cfg, payload):
    if len(payload) != 8:
        return
    page = payload[0]
    if page == FEATURE_PAGE_BASIC:
        ax = payload[1]
        fl = payload[2]
        cfg["cursorXAxis"]      = ax & 0x03
        cfg["cursorYAxis"]      = (ax >> 2) & 0x03
        cfg["clickAxis"]        = (ax >> 4) & 0x03
        cfg["invertX"]          = bool(fl & 0x01)
        cfg["invertY"]          = bool(fl & 0x02)
        cfg["invertClick"]      = bool(fl & 0x04)
        cfg["enableClicks"]     = bool(fl & 0x08)
        cfg["enableFlick"]      = bool(fl & 0x10)
        cfg["enableShake"]      = bool(fl & 0x20)
        cfg["enableDoubleTilt"] = bool(fl & 0x40)
        cfg["enableCircle"]     = bool(fl & 0x80)
        cfg["deadzoneX"]        = payload[3] / 10.0
        cfg["deadzoneY"]        = payload[4] / 10.0
        cfg["deadzoneClick"]    = payload[5] / 10.0
        cfg["tiltThreshDeg"]    = payload[6] / 10.0
    elif page == FEATURE_PAGE_GAINS:
        cfg["gainX"] = _get16(payload, 1) / 100.0
        cfg["gainY"] = _get16(payload, 3) / 100.0
    elif page == FEATURE_PAGE_FLICK:
        cfg["flickVelThresh"] = float(_get16(payload, 1))
        cfg["flickReturnDeg"] = payload[3] / 10.0
        cfg["flickConfirmMs"] = float(_get16(payload, 4))
    elif page == FEATURE_PAGE_OTHER_GESTURES:
        cfg["shakeVelThresh"] = float(_get16(payload, 1))
        cfg["doubleTiltDeg"]  = payload[3] / 10.0
        cfg["circleMinSpeed"] = float(_get16(payload, 4))


def decode_gesture_report(report):
    data = list(report)
    if len(data) >= 3 and data[0] == REPORT_ID_GESTURE:
        gid, gdata = data[1], data[2]
    elif len(data) >= 2:
        gid, gdata = data[0], data[1]
    else:
        return None
    name     = GESTURE_NAMES.get(gid, f"Gesture{gid}")
    axis_idx = gdata & 0x03
    axis     = AXIS_SHORT[axis_idx] if axis_idx < len(AXIS_SHORT) else ""
    if gid in (1, 3) and axis:
        axis += "+" if (gdata & 0x80) else "-"
    return {"gesture": name, "axis": axis}


# ─────────────────────────────────────────────────────────────────────────────
# HID thread — reads gesture reports + exposes feature report R/W
# ─────────────────────────────────────────────────────────────────────────────
class HIDThread(threading.Thread):
    def __init__(self, on_gesture, on_status):
        super().__init__(daemon=True)
        self.on_gesture = on_gesture
        self.on_status  = on_status
        self._stop      = threading.Event()
        self.dev_gesture = None
        self.dev_feature = None
        self.connected  = False
        self._lock      = threading.Lock()

    def _open_device(self):
        if hid is None:
            raise RuntimeError(hid_import_diagnostic())
        matches = hid.enumerate()
        
        path_gesture = None
        path_feature = None
        
        for item in matches:
            if HID_PRODUCT.lower() in (item.get("product_string") or "").lower():
                # On Windows, HID collections are split into multiple device paths.
                if item.get("usage_page") == 65280 and item.get("usage") == 1:
                    path_gesture = item["path"]
                elif item.get("usage_page") == 65280 and item.get("usage") == 16:
                    path_feature = item["path"]
                else:
                    if not path_gesture: path_gesture = item["path"]
                    if not path_feature: path_feature = item["path"]

        if not path_gesture or not path_feature:
            raise RuntimeError(
                "ESP32 MPU Mouse not found.\n"
                "Make sure it is paired and connected via Bluetooth."
            )
            
        def _open_path(path):
            if hasattr(hid, 'Device'):
                d = hid.Device(path=path)
                d.nonblocking = True
                return d
            else:
                d = hid.device()
                d.open_path(path)
                d.set_nonblocking(True)
                return d

        self.dev_gesture = _open_path(path_gesture)
        if path_feature == path_gesture:
            self.dev_feature = self.dev_gesture
        else:
            self.dev_feature = _open_path(path_feature)

    def run(self):
        try:
            self._open_device()
            self.connected = True
            self.on_status("connected")
            while not self._stop.is_set():
                with self._lock:
                    report = self.dev_gesture.read(64)
                if report:
                    msg = decode_gesture_report(report)
                    if msg:
                        self.on_gesture(msg)
                time.sleep(0.02)
        except Exception as e:
            self.on_status(f"error:{e}")
        finally:
            self.connected = False
            try:
                if self.dev_gesture: self.dev_gesture.close()
                if self.dev_feature and self.dev_feature != self.dev_gesture:
                    self.dev_feature.close()
            except Exception:
                pass
            self.on_status("disconnected")

    def send_feature(self, payload8):
        if not self.connected or not self.dev_feature:
            raise RuntimeError("Device not connected")
        if len(payload8) != 8:
            raise ValueError("Feature payload must be exactly 8 bytes")
        with self._lock:
            return self.dev_feature.send_feature_report(
                bytes([REPORT_ID_FEATURE] + list(payload8))
            )

    def get_feature(self, page):
        self.send_feature([FEATURE_PAGE_SELECT, page, 0, 0, 0, 0, 0, 0])
        time.sleep(0.03)
        with self._lock:
            data = list(self.dev_feature.get_feature_report(REPORT_ID_FEATURE, 9))
        if len(data) >= 9 and data[0] == REPORT_ID_FEATURE:
            return data[1:9]
        if len(data) >= 8:
            return data[:8]
        raise RuntimeError("Short feature report received")

    def stop(self):
        self._stop.set()


class GlobalHotkeyListener(threading.Thread):
    """Windows-only global hotkey listener using RegisterHotKey."""

    WM_HOTKEY = 0x0312
    WM_NULL = 0x0000

    def __init__(self, callback, modifiers, vk, hotkey_id=1):
        super().__init__(daemon=True)
        self._callback = callback
        self._modifiers = modifiers
        self._vk = vk
        self._hotkey_id = hotkey_id
        self._registered = False
        self._thread_id = None
        self.error = None

    def run(self):
        if not hasattr(ctypes, "windll"):
            self.error = "Global hotkey is only supported on Windows."
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, self._hotkey_id, self._modifiers, self._vk):
            self.error = "Could not register global hotkey (already in use)."
            return
        self._registered = True

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY and msg.wParam == self._hotkey_id:
                try:
                    self._callback()
                except Exception:
                    pass

        if self._registered:
            user32.UnregisterHotKey(None, self._hotkey_id)
            self._registered = False

    def stop(self):
        if not hasattr(ctypes, "windll"):
            return
        user32 = ctypes.windll.user32
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, self.WM_NULL, 0, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Custom widgets
# ─────────────────────────────────────────────────────────────────────────────
class PrecisionSlider(tk.Frame):
    """Horizontal slider + spinbox for coarse drag and precise keyboard entry."""

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
        try:
            float(P); return True
        except ValueError:
            return False

    def _on_var(self, *_):
        if self._updating: return
        self._updating = True
        try:
            v = round(float(self._var.get()), self._dec())
            v = max(self._from, min(self._to, v))
            if self._cb: self._cb(v)
        except (tk.TclError, ValueError):
            pass
        finally:
            self._updating = False

    def get(self):
        try:   return round(float(self._var.get()), self._dec())
        except: return self._from

    def set(self, v):
        self._var.set(round(float(v), self._dec()))


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
        self.create_text(cx, self.H-5, text=f"{sign}{self._value:.1f}°",
                         fill=C["text1"], font=FONT_TINY)
        self.create_text(cx, 7, text=self._label,
                         fill=C["accent"] if not inside else C["text2"],
                         font=FONT_TINY)


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
        col = (C["text2"] if inside
               else (C["green"] if abs(self._value) < 45 else C["amber"]))
        if norm >= 0:
            self.create_rectangle(cx, cy-3, cx+bw, cy+3, fill=col, outline="")
        else:
            self.create_rectangle(cx-bw, cy-3, cx, cy+3, fill=col, outline="")
        nx = cx + int(norm * (cx - 4))
        self.create_rectangle(nx-1, 2, nx+1, self.H-2, fill=C["accent"], outline="")
        sign = "+" if self._value >= 0 else ""
        self.create_text(self.W-3, cy, text=f"{sign}{self._value:6.1f}°",
                         anchor="e", fill=C["text0"], font=FONT_MONO)


class Card(tk.Frame):
    def __init__(self, parent, title, **kw):
        super().__init__(parent, bg=C["bg1"],
                         highlightthickness=1,
                         highlightbackground=C["border"], **kw)
        hdr = tk.Frame(self, bg=C["bg0"])
        hdr.pack(fill="x")
        tk.Frame(hdr, bg=C["accent"], width=3).pack(side="left", fill="y")
        tk.Label(hdr, text=f"  {title}", bg=C["bg0"], fg=C["text0"],
                 font=FONT_HEAD, pady=5).pack(side="left")
        self.body = tk.Frame(self, bg=C["bg1"])
        self.body.pack(fill="both", expand=True, padx=10, pady=8)


# ─────────────────────────────────────────────────────────────────────────────
# Main application
# ─────────────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MPU Mouse Config")
        self.configure(bg=C["bg0"])
        self.minsize(880, 620)

        self.cfg        = dict(DEFAULT_CFG)
        self.hid_thread = None
        self.hotkey_listener = None
        self.axes       = [0.0, 0.0, 0.0]
        self._auto_recenter_job = None

        self._load_config()
        self._setup_style()
        self._build_ui()
        self._apply_cfg_to_widgets()
        self._setup_hotkey()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._live_loop()
        self.after(300, self._auto_connect_loop)

    def _setup_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TCombobox",
                    fieldbackground=C["bg3"], background=C["bg3"],
                    foreground=C["text0"], selectbackground=C["accent2"],
                    bordercolor=C["border"], arrowcolor=C["text1"],
                    relief="flat", padding=(4, 3))
        s.map("TCombobox",
              fieldbackground=[("readonly", C["bg3"])],
              foreground=[("readonly", C["text0"])])

    # ── Build UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg=C["bg0"],
                       highlightthickness=1, highlightbackground=C["border"])
        top.pack(fill="x")

        tk.Label(top, text="MPU MOUSE  CONFIG",
                 bg=C["bg0"], fg=C["accent"],
                 font=("Consolas", 11, "bold"), padx=14, pady=8).pack(side="left")

        tk.Frame(top, bg=C["border"], width=1).pack(
            side="left", fill="y", pady=4)

        # Connect button — the only connection control needed
        self._btn(top, "CONNECT", self._connect, accent=True).pack(
            side="left", padx=10)

        self._btn(top, "RECENTER", self._recenter, accent=False).pack(
            side="left", padx=(0, 10))

        self.led = tk.Canvas(top, width=10, height=10,
                             bg=C["bg0"], highlightthickness=0)
        self.led.pack(side="left", padx=(4, 2))
        self._led = self.led.create_oval(1, 1, 9, 9, fill=C["red"], outline="")

        self.status_lbl = tk.Label(top, text="DISCONNECTED",
                                   bg=C["bg0"], fg=C["red"],
                                   font=("Consolas", 8, "bold"), padx=8)
        self.status_lbl.pack(side="left")

        tk.Label(top, text=f"HOTKEY: {HOTKEY_LABEL} (RECENTER)",
             bg=C["bg0"], fg=C["text1"],
             font=("Consolas", 8), padx=10).pack(side="right")

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
        for r in range(3): left.rowconfigure(r, weight=[0, 0, 1][r])
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=0)
        right.rowconfigure(1, weight=1)

        Card(left, "AXIS MAPPING").grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self._build_mapping(left.winfo_children()[-1].body)

        Card(left, "DEADZONES  &  SENSITIVITY").grid(row=1, column=0, sticky="ew", pady=(0, 5))
        self._build_deadzones(left.winfo_children()[-1].body)

        Card(left, "GESTURE THRESHOLDS").grid(row=2, column=0, sticky="nsew")
        self._build_gestures(left.winfo_children()[-1].body)

        Card(right, "LIVE MONITOR").grid(row=0, column=0, sticky="ew", pady=(0, 5))
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
            ("APPLY",          self._apply,          True),
            ("LOAD FROM DEVICE", self._load_and_apply, False),
            ("DEFAULTS", self._reset_defaults, False),
        ]:
            self._btn(inner, text, cmd, accent=acc).pack(side="left", padx=3)

    # ── Section builders ──────────────────────────────────────────────────────
    def _build_mapping(self, p):
        self.axis_vars, self.invert_vars = {}, {}
        rows = [
            ("cursorXAxis", "CURSOR X",  "invertX",     "INV X"),
            ("cursorYAxis", "CURSOR Y",  "invertY",     "INV Y"),
            ("clickAxis",   "CLICK",     "invertClick", "INV CLK"),
        ]
        for r, (ak, al, ik, il) in enumerate(rows):
            tk.Label(p, text=al, bg=C["bg1"], fg=C["text1"],
                     font=FONT_TINY, width=9, anchor="w").grid(
                         row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            ttk.Combobox(p, textvariable=var, values=AXIS_NAMES,
                         width=14, state="readonly").grid(
                             row=r, column=1, sticky="w", padx=(4, 16))
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
            v  = DeadzoneViz(cell, lbl); v.pack()
            sl = PrecisionSlider(cell, 0.0, 20.0, 0.1,
                                 initial=self.cfg.get(key, 1.5), width=70,
                                 on_change=lambda val, k=key: self._on_dead(k, val))
            sl.pack(pady=(3, 0))
            self.dead_vizzes[key]  = v
            self.dead_sliders[key] = sl
        tk.Frame(p, bg=C["border"], height=1).pack(fill="x", pady=4)
        for key, lbl in [("gainX","GAIN X"),("gainY","GAIN Y")]:
            row = tk.Frame(p, bg=C["bg1"]); row.pack(fill="x", pady=2)
            tk.Label(row, text=lbl, bg=C["bg1"], fg=C["text1"],
                     font=FONT_TINY, width=9, anchor="w").pack(side="left")
            sl = PrecisionSlider(row, 0.05, 3.0, 0.05,
                                 initial=self.cfg.get(key, 0.3), width=160)
            sl.pack(side="left", padx=4)
            self.gain_sliders[key] = sl

    def _build_gestures(self, p):
        p.columnconfigure(0, weight=1)
        self.thresh_sliders      = {}
        self.gesture_toggle_vars = {}
        toggles = tk.Frame(p, bg=C["bg1"]); toggles.pack(fill="x", pady=(0,6))
        toggles.columnconfigure(0, weight=1); toggles.columnconfigure(1, weight=1)
        for i, (key, label) in enumerate([
            ("enableClicks",     "Enable Clicks"),
            ("enableFlick",      "Enable Flick"),
            ("enableShake",      "Enable Shake"),
            ("enableDoubleTilt", "Enable Double-Tilt"),
            ("enableCircle",     "Enable Circle"),
        ]):
            var = tk.BooleanVar(value=self.cfg.get(key, True))
            tk.Checkbutton(toggles, text=label, variable=var,
                           bg=C["bg1"], fg=C["text1"], selectcolor=C["bg3"],
                           activebackground=C["bg1"], font=FONT_TINY,
                           relief="flat", bd=0, highlightthickness=0).grid(
                               row=i//2, column=i%2, sticky="w", pady=1, padx=(0,8))
            self.gesture_toggle_vars[key] = var
        tk.Frame(p, bg=C["border"], height=1).pack(fill="x", pady=(2,6))
        for key, lbl, unit, lo, hi, res in [
            ("tiltThreshDeg",  "Tilt threshold", "°",    5,  90,  0.5),
            ("flickVelThresh", "Flick speed",    "°/s", 40, 400,  5.0),
            ("flickReturnDeg", "Flick return",   "°",    1,  30,  0.5),
            ("flickConfirmMs", "Flick window",   "ms",  80, 800, 10.0),
            ("shakeVelThresh", "Shake speed",    "°/s", 20, 300,  5.0),
            ("doubleTiltDeg",  "Double-tilt",    "°",    5,  80,  0.5),
            ("circleMinSpeed", "Circle speed",   "°/s",  5, 120,  1.0),
        ]:
            row = tk.Frame(p, bg=C["bg1"]); row.pack(fill="x", pady=2)
            row.columnconfigure(1, weight=1)
            tk.Label(row, text=f"{lbl} ({unit})", bg=C["bg1"], fg=C["text1"],
                     font=FONT_TINY, width=22, anchor="w").pack(side="left")
            sl = PrecisionSlider(row, lo, hi, res,
                                 initial=self.cfg.get(key, lo), width=0)
            sl.pack(side="left", fill="x", expand=True)
            self.thresh_sliders[key] = sl

    def _build_live(self, p):
        self.axis_bars = []
        for name in ["YAW", "PITCH", "ROLL"]:
            row = tk.Frame(p, bg=C["bg1"]); row.pack(fill="x", pady=2)
            row.columnconfigure(1, weight=1)
            tk.Label(row, text=name, bg=C["bg1"], fg=C["accent"],
                     font=("Consolas", 8, "bold"), width=6, anchor="w").pack(side="left")
            bar = AxisBar(row); bar.pack(side="left", fill="x", expand=True)
            self.axis_bars.append(bar)

    def _build_log(self, p):
        p.columnconfigure(0, weight=1); p.rowconfigure(0, weight=1)
        frame = tk.Frame(p, bg=C["bg1"]); frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1); frame.rowconfigure(0, weight=1)
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
            d = next(
                (self.cfg.get(k, 0) for k, ax in dead_axis.items() if ax == i),
                0.0
            )
            bar.update(v, d)
        for key, viz in self.dead_vizzes.items():
            ax_key = {"deadzoneX":"cursorXAxis",
                      "deadzoneY":"cursorYAxis",
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

    def _setup_hotkey(self):
        """Register a system-wide RECENTER shortcut on Windows."""
        self.hotkey_listener = GlobalHotkeyListener(
            callback=lambda: self.after(0, lambda: self._recenter(silent=True)),
            modifiers=HOTKEY_MODIFIERS,
            vk=HOTKEY_VK_Q,
        )
        self.hotkey_listener.start()

        def _check_hotkey_status():
            if self.hotkey_listener and self.hotkey_listener.error:
                messagebox.showwarning(
                    "Hotkey unavailable",
                    f"Global hotkey {HOTKEY_LABEL} could not be registered.\n"
                    "It may already be used by another app."
                )

        self.after(300, _check_hotkey_status)

    # ── Widget ↔ cfg ──────────────────────────────────────────────────────────
    def _apply_cfg_to_widgets(self):
        for k, v in self.axis_vars.items():
            v.set(AXIS_NAMES[min(self.cfg.get(k, 0), len(AXIS_NAMES)-1)])
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
        for k, v in self.invert_vars.items():   self.cfg[k] = v.get()
        for k, v in self.gesture_toggle_vars.items(): self.cfg[k] = v.get()
        for k, sl in self.dead_sliders.items():  self.cfg[k] = sl.get()
        for k, sl in self.gain_sliders.items():  self.cfg[k] = sl.get()
        for k, sl in self.thresh_sliders.items(): self.cfg[k] = sl.get()

    # ── Callbacks ─────────────────────────────────────────────────────────────
    def _on_gesture(self, msg): self.after(0, lambda: self._log_gesture(msg))
    def _on_status(self, code): self.after(0, lambda: self._set_status(code))

    def _set_status(self, code):
        if   code == "connected":
            self._start_connect_calibration()
            return
        elif code == "connecting":      text, col = "CONNECTING...", C["amber"]
        elif code == "calibrating":     text, col = "CALIBRATING... STAY STILL", C["amber"]
        elif code == "waiting":         text, col = "WAITING FOR BLUETOOTH", C["text1"]
        elif code == "ack":             text, col = "CFG APPLIED ✓", C["green"]
        elif code.startswith("error:"):
            msg = code[6:].strip().lower()
            if "not found" in msg or "paired and connected" in msg:
                text, col = "WAITING FOR BLUETOOTH", C["text1"]
            else:
                text, col = "ERROR", C["red"]
        else:                           text, col = "DISCONNECTED",  C["red"]
        self.status_lbl.configure(text=text, fg=col)
        self.led.itemconfig(self._led, fill=col)

    def _start_connect_calibration(self):
        self._set_status("calibrating")
        self._log_info("Calibrating: keep device still for a few seconds...")
        if self._auto_recenter_job is not None:
            try:
                self.after_cancel(self._auto_recenter_job)
            except Exception:
                pass
        self._auto_recenter_job = self.after(
            AUTO_CALIBRATION_MS,
            lambda: self._recenter(silent=True)
        )

    def _auto_connect_loop(self):
        if not self.winfo_exists():
            return
        if hid is None:
            self._set_status("error:hid library unavailable")
        else:
            if self.hid_thread and not self.hid_thread.is_alive():
                self.hid_thread = None
            if not self.hid_thread:
                self._set_status("waiting")
                self._connect(silent=True)
        self.after(AUTO_CONNECT_POLL_MS, self._auto_connect_loop)

    def _log_info(self, text):
        line = f"[{time.strftime('%H:%M:%S')}]  {text}\n"
        self.gesture_log.configure(state="normal")
        self.gesture_log.insert("end", line)
        self.gesture_log.see("end")
        self.gesture_log.configure(state="disabled")

    def _log_gesture(self, msg):
        text = (f"[{time.strftime('%H:%M:%S')}]  "
                f"{msg.get('gesture','?'):<12}{msg.get('axis','')}\n")
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
    def _connect(self, silent=False):
        """Open the HID interface. The device must already be BT-paired."""
        if hid is None:
            if not silent:
                messagebox.showerror("HID library error", hid_import_diagnostic())
            return
        if self.hid_thread and self.hid_thread.connected:
            return
        if self.hid_thread and self.hid_thread.is_alive():
            return
        if self.hid_thread:
            self.hid_thread.stop()
            self.hid_thread = None

        self._set_status("connecting")
        self.hid_thread = HIDThread(self._on_gesture, self._on_status)
        self.hid_thread.start()

    def _apply(self):
        """Push current widget values to the device via HID feature reports."""
        self._widgets_to_cfg()
        if not self.hid_thread or not self.hid_thread.connected:
            messagebox.showinfo(
                "Not connected",
                "Click CONNECT first.\n\n"
                "Make sure the ESP32 MPU Mouse is paired and connected "
                "in Windows Bluetooth settings."
            )
            return
        pages = cfg_to_feature_pages(self.cfg)

        def _send():
            try:
                for page in pages:
                    self.hid_thread.send_feature(page)
                    time.sleep(0.02)
                self._persist_cfg()
                self.after(0, lambda: self._set_status("ack"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("HID error", str(e)))
                self.after(0, lambda: self._set_status(f"error:{e}"))

        threading.Thread(target=_send, daemon=True).start()

    def _recenter(self, silent=False):
        """Send command to recenter the device's idle position."""
        if not self.hid_thread or not self.hid_thread.connected:
            if not silent:
                messagebox.showinfo("Not connected", "Connect the device first.")
            return
        def _send_recenter():
            try:
                self.hid_thread.send_feature([0x7E, 1, 0, 0, 0, 0, 0, 0])
                self.after(0, lambda: self._set_status("ack"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("HID error", str(e)))
        threading.Thread(target=_send_recenter, daemon=True).start()

    def _persist_cfg(self):
        try:
            with open(SAVE_FILE, "w") as f:
                json.dump(self.cfg, f, indent=2)
        except OSError as e:
            raise RuntimeError(str(e))

    def _load_config(self):
        try:
            with open(SAVE_FILE) as f:
                self.cfg.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        # Migrate old key name
        if "tiltThreshDeg" not in self.cfg and "clickThreshDeg" in self.cfg:
            self.cfg["tiltThreshDeg"] = self.cfg.pop("clickThreshDeg")
        self.cfg.pop("clickThreshDeg", None)

    def _load_and_apply(self):
        """Read current config back from the device via HID feature reports."""
        if not self.hid_thread or not self.hid_thread.connected:
            # Fall back to JSON file if not connected
            self._load_config()
            self._apply_cfg_to_widgets()
            return
        try:
            for page in (FEATURE_PAGE_BASIC, FEATURE_PAGE_GAINS,
                         FEATURE_PAGE_FLICK, FEATURE_PAGE_OTHER_GESTURES):
                merge_feature_page(self.cfg, self.hid_thread.get_feature(page))
            self._persist_cfg()
            self._apply_cfg_to_widgets()
            self._set_status("ack")
        except Exception as e:
            messagebox.showerror("HID error", str(e))
            self._set_status(f"error:{e}")

    def _reset_defaults(self):
        self.cfg = dict(DEFAULT_CFG)
        self._apply_cfg_to_widgets()
        if self.hid_thread and self.hid_thread.connected:
            self._apply()
            return
        try:
            self._persist_cfg()
            self._set_status("ack")
        except Exception as e:
            messagebox.showerror("Save error", str(e))

    def _on_close(self):
        if self.hid_thread: self.hid_thread.stop()
        if self.hotkey_listener: self.hotkey_listener.stop()
        if self._auto_recenter_job is not None:
            try:
                self.after_cancel(self._auto_recenter_job)
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()