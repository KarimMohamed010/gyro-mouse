"""
Dwell Click System — v4
========================
Accessibility dwell-clicking for air mouse / head tracking.

Install:   pip install PySide6 pynput
Run:       python dwell_click_system.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 RIGHT EDGE PANEL  (always visible, vertically centered)
 ┌──────────┐
 │    ▲     │  scroll up
 │   [●]    │  main arm button  (≡ idle / L armed / R armed)
 │    ▼     │  scroll down
 └──────────┘
 Entire panel = SAFE ZONE: cursor ring hidden while inside.

 LEFT EDGE PANEL  (always visible, vertically centered)
 ┌──────────┐
 │   [⧉]   │  double-click  (always active, no arm needed)
 │   [↕]   │  drag / drop   (always active, no arm needed)
 │   [⌨]   │  keyboard      (toggle dwell keyboard overlay)
 └──────────┘
 Also a SAFE ZONE.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 MAIN BUTTON  (right edge center)
   1st dwell → ARMED LEFT  (green — cursor dwell fires LMB)
   2nd dwell → ARMED RIGHT (amber — cursor dwell fires RMB)
   3rd dwell → IDLE (disarm)

 SCROLL  (above/below main button)
   Hover → ring loads → scrolling begins → accelerates
   Leave → instant stop

 DOUBLE-CLICK  (left panel, always on)
   Dwell → fires double-click at current cursor position
   Works regardless of arm state

 DRAG / DROP  (left panel, always on)
   1st dwell → mouse-down held at cursor position
   Move cursor to destination
   2nd dwell → mouse-up (drop)

 KEYBOARD  (left panel, always on)
   Dwell → floating keyboard appears near cursor
   Dwell any key → types that character (auto-fires, no arm needed)
   Hover keyboard button again → keyboard closes
   Keyboard is its own safe zone (ring hidden inside keyboard)

 CURSOR RING
   Green = left-click  |  Amber = right-click
   HIDDEN while cursor is in any safe zone (right panel, left panel,
   keyboard overlay) — no double-loading visual confusion
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import ctypes
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import time
import socket
import threading
from enum import Enum, auto

from ctypes import wintypes

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore    import Qt, QTimer, QRectF, QPointF, Signal, QObject
from PySide6.QtGui     import (
    QPainter, QColor, QPen, QBrush, QFont,
    QRadialGradient, QCursor
)

from modern_keyboard import KeyboardManager as ModernKeyboardManager

try:
    from pynput.mouse    import Button, Controller as MouseController
    from pynput.keyboard import Controller as KbController, Key
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    print("[DwellClick] pynput not found — actions simulated. pip install pynput")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

DWELL_EDGE          = 0.75   # s  arm / charge main button
DWELL_CLICK         = 1.2    # s  fire cursor click
DWELL_SCROLL_LOAD   = 0.55   # s  fill scroll circle
DWELL_ACTION        = 0.85   # s  double-click / drag / keyboard trigger
DWELL_KEY           = 0.7    # s  type a key on the dwell keyboard
ARMED_TIMEOUT       = 7.0    # s  auto-disarm

MOVE_THRESHOLD      = 6      # px
PREDWELL_DELAY      = 0.18   # s
CLICK_COOLDOWN      = 0.30   # s

SCROLL_TICK_MS      = 100    # ms
SCROLL_SPEED_INIT   = 2
SCROLL_SPEED_MAX    = 7
SCROLL_ACCEL_TIME   = 2.5    # s

# Layout
MAIN_BTN_R          = 26     # px
SCROLL_BTN_R        = 16     # px
ACTION_BTN_R        = 19     # px
BTN_GAP             = 10     # px
RIGHT_PAD           = 4      # px panel from right edge
LEFT_PAD            = 4      # px panel from left edge

# ─────────────────────────────────────────────────────────────────────────────
# LIVE CONFIG LOADER  (reads mpu_config.json written by the Tkinter GUI)
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "overlay_config.json"
_cfg_cache   = {}     # last loaded values
_cfg_mtime   = 0.0    # file modification time

def load_overlay_config():
    """Read overlay-relevant keys from the shared config file."""
    global _cfg_cache, _cfg_mtime
    try:
        mt = _CONFIG_PATH.stat().st_mtime
        if mt == _cfg_mtime:
            return _cfg_cache
        with open(_CONFIG_PATH) as f:
            data = json.load(f)
        _cfg_mtime = mt
        _cfg_cache = {
            "scrollSpeed":      int(data.get("scrollSpeed", SCROLL_SPEED_INIT)),
            "overlayIconRadius": int(data.get("overlayIconRadius", ACTION_BTN_R)),
            "dwellTime":        float(data.get("dwellTime", DWELL_EDGE)),
            "rcIconX":          int(data.get("rcIconX", -1)),
            "rcIconY":          int(data.get("rcIconY", -1)),
            "dragIconX":        int(data.get("dragIconX", -1)),
            "dragIconY":        int(data.get("dragIconY", -1)),
            "kbIconX":          int(data.get("kbIconX", -1)),
            "kbIconY":          int(data.get("kbIconY", -1)),
            "copyIconX":        int(data.get("copyIconX", -1)),
            "copyIconY":        int(data.get("copyIconY", -1)),
            "pasteIconX":       int(data.get("pasteIconX", -1)),
            "pasteIconY":       int(data.get("pasteIconY", -1)),
            "mainBtnX":         int(data.get("mainBtnX", -1)),
            "mainBtnY":         int(data.get("mainBtnY", -1)),
            "scrollUpX":        int(data.get("scrollUpX", -1)),
            "scrollUpY":        int(data.get("scrollUpY", -1)),
            "scrollDownX":      int(data.get("scrollDownX", -1)),
            "scrollDownY":      int(data.get("scrollDownY", -1)),
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        if not _cfg_cache:
            _cfg_cache = {
                "scrollSpeed":       SCROLL_SPEED_INIT,
                "overlayIconRadius": ACTION_BTN_R,
                "dwellTime":         DWELL_EDGE,
                "rcIconX": -1, "rcIconY": -1,
                "dragIconX": -1, "dragIconY": -1,
                "kbIconX": -1, "kbIconY": -1,
                "copyIconX": -1, "copyIconY": -1,
                "pasteIconX": -1, "pasteIconY": -1,
                "mainBtnX": -1, "mainBtnY": -1,
                "scrollUpX": -1, "scrollUpY": -1,
                "scrollDownX": -1, "scrollDownY": -1,
            }
    return _cfg_cache

CURSOR_RING_R       = 26
CURSOR_RING_W       = 3
TIMER_MS            = 16

# Keyboard
KB_KEY_SIZE         = 46     # px key width/height
KB_KEY_GAP          = 5      # px between keys
KB_ROWS = [
    ["Q","W","E","R","T","Y","U","I","O","P"],
    ["A","S","D","F","G","H","J","K","L"],
    ["Z","X","C","V","B","N","M","⌫"],
    ["123", "Space", "⏎"],
]
KB_SPECIAL = {
    "⌫":    "backspace",
    "⏎":    "enter",
    "Space": "space",
    "123":   "toggle_num",
}
KB_NUM_ROWS = [
    ["1","2","3","4","5","6","7","8","9","0"],
    ["!","@","#","$","%","^","&","*","(",")"],
    ["-","_","=","+","[","]",";","'"],
    ["ABC", "Space", "⏎"],
]

# ─── Palette ──────────────────────────────────────────────────────────────────
C_IDLE      = QColor(48,  50,  60,  138)
C_HOVER_L   = QColor(55,  138, 255, 188)
C_ARMED_L   = QColor(52,  200, 112, 208)
C_HOVER_R   = QColor(255, 152, 44,  188)
C_ARMED_R   = QColor(232, 122, 36,  208)
C_SCROLL    = QColor(88,  162, 255, 182)
C_SCROLL_A  = QColor(255, 178, 58,  208)
C_ACTION    = QColor(155, 108, 252, 182)
C_DRAG_HELD = QColor(252,  88,  88, 208)
C_KB_BTN    = QColor(155, 108, 252, 182)
C_RING_L    = QColor(58,  208, 118, 255)
C_RING_R    = QColor(238, 128,  38, 255)
C_RING_BG   = QColor(255, 255, 255,  20)
C_SAFE_PILL = QColor(255, 255, 255,  12)

# Keyboard colors
C_KB_BG     = QColor(22,  24,  32,  230)
C_KB_KEY    = QColor(48,  52,  68,  230)
C_KB_HOVER  = QColor(80,  100, 200, 240)
C_KB_SPEC   = QColor(36,  40,  56,  240)
C_KB_PROG   = QColor(100, 160, 255, 255)
C_KB_TEXT   = QColor(235, 235, 240, 255)


# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────

class State(Enum):
    IDLE     = auto()
    ARMED_L  = auto()
    ARMED_R  = auto()
    ARMED_DRAG = auto()
    CLICK    = auto()
    DRAGGING = auto()


class DragPhase(Enum):
    IDLE = auto()
    HELD = auto()


# ─────────────────────────────────────────────────────────────────────────────
# DwellTracker
# ─────────────────────────────────────────────────────────────────────────────

class DwellTracker(QObject):
    progress  = Signal(float)
    complete  = Signal(float, float)
    cancelled = Signal()

    def __init__(self, dwell_time: float, once_only: bool = True):
        super().__init__()
        self.dwell_time = dwell_time
        self.once_only  = once_only
        self._lx = self._ly = 0
        self._stable_t  = None
        self._dwell_t   = None
        self._last_fire = 0.0
        self._fired     = False

    def reset(self):
        self._stable_t = None
        self._dwell_t  = None
        self._fired    = False

    def update(self, x: int, y: int):
        if self.once_only and self._fired:
            return
        now  = time.monotonic()
        dist = math.hypot(x - self._lx, y - self._ly)
        self._lx, self._ly = x, y

        if dist > MOVE_THRESHOLD:
            if self._dwell_t is not None:
                self.cancelled.emit()
            self.reset()
            self._stable_t = now
            return

        if self._stable_t is None:
            self._stable_t = now
            return
        if now - self._stable_t < PREDWELL_DELAY:
            return
        if self._dwell_t is None:
            self._dwell_t = now

        elapsed = now - self._dwell_t
        self.progress.emit(min(elapsed / self.dwell_time, 1.0))

        if elapsed >= self.dwell_time:
            if now - self._last_fire < CLICK_COOLDOWN:
                return
            self._last_fire = now
            if self.once_only:
                self._fired = True
            self.reset()
            self.complete.emit(float(x), float(y))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def draw_ring(p: QPainter, cx, cy, r, prog, fg, lw=2.4):
    rect = QRectF(cx - r, cy - r, r * 2, r * 2)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(C_RING_BG, lw, Qt.SolidLine, Qt.RoundCap))
    p.drawEllipse(rect)
    if prog > 0:
        p.setPen(QPen(fg, lw, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 90 * 16, -int(prog * 360 * 16))

def draw_dot(p: QPainter, cx, cy, r, color):
    p.setBrush(QBrush(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(cx, cy), r, r)


if sys.platform == "win32":
    USER32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
else:
    USER32 = None
    WM_CLOSE = 0


def ring_color_for_state(state: State) -> QColor | None:
    if state == State.ARMED_L:
        return QColor(C_RING_L)
    if state == State.ARMED_R:
        return QColor(C_RING_R)
    if state in (State.ARMED_DRAG, State.DRAGGING):
        return QColor(C_DRAG_HELD)
    return None


class SystemKeyboardBridge(QObject):
    """Thin wrapper around the Windows on-screen keyboard."""

    def __init__(self):
        super().__init__()
        self._exe = self._resolve_exe()
        self._proc = None

    def _resolve_exe(self):
        if sys.platform != "win32":
            return None
        candidates = [
            shutil.which("osk.exe"),
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "osk.exe"),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    def available(self) -> bool:
        return bool(self._exe)

    def uses_external_dwell(self) -> bool:
        return self.isVisible()

    def show_near(self, _x: int, _y: int) -> bool:
        if not self.available():
            return False
        if self.isVisible():
            return True
        try:
            self._proc = subprocess.Popen([self._exe])
        except OSError as exc:
            print(f"[DwellClick] System keyboard launch failed: {exc}")
            self._proc = None
            return False
        return True

    def hide(self):
        hwnd = self._find_window()
        if hwnd:
            USER32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self._proc = None

    def update_cursor(self, _x: int, _y: int):
        pass

    def contains(self, x: int, y: int) -> bool:
        rect = self.geometry()
        if not rect:
            return False
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def geometry(self):
        hwnd = self._find_window()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def isVisible(self) -> bool:
        return self.geometry() is not None

    def _find_window(self):
        if not self.available():
            return 0
        hwnd = USER32.FindWindowW("OSKMainClass", None)
        if hwnd and USER32.IsWindowVisible(hwnd):
            return hwnd
        return 0


class KeyboardManager(QObject):
    """Uses the system keyboard when available, otherwise falls back in-app."""

    def __init__(self):
        super().__init__()
        self._system = SystemKeyboardBridge()
        self._fallback = DwellKeyboard()
        self._mode = "system" if self._system.available() else "fallback"

    def show_near(self, x: int, y: int):
        if self._mode == "system":
            if self._system.show_near(x, y):
                return
            print("[DwellClick] Falling back to built-in dwell keyboard.")
            self._mode = "fallback"
        self._fallback.show_near(x, y)

    def hide(self):
        self._system.hide()
        self._fallback.hide()

    def contains(self, x: int, y: int) -> bool:
        if self._mode == "system":
            return self._system.contains(x, y)
        return self._fallback.contains(x, y)

    def update_cursor(self, x: int, y: int):
        if self._mode == "fallback":
            self._fallback.update_cursor(x, y)
        else:
            self._system.update_cursor(x, y)

    def isVisible(self) -> bool:
        if self._mode == "system":
            return self._system.isVisible()
        return self._fallback.isVisible()

    def uses_external_dwell(self) -> bool:
        return self._mode == "system" and self._system.uses_external_dwell()


# ─────────────────────────────────────────────────────────────────────────────
# DwellKeyboard — floating keyboard overlay
# ─────────────────────────────────────────────────────────────────────────────

class DwellKeyboard(QWidget):
    """
    Floating keyboard that appears near the cursor.
    Each key has its own DwellTracker — hover a key → ring fills → types it.
    No arm state needed. Keyboard is its own safe zone.
    Signals: closed() when user dwells the keyboard trigger again.
    """

    closed = Signal()

    def __init__(self):
        super().__init__()
        self._numeric   = False
        self._keys      = []          # list of (label, rect_local)
        self._prog      = {}          # label → progress 0..1
        self._hover_key = None
        self._trackers  = {}          # label → DwellTracker
        self._last_typed_at = 0.0

        # Build layout
        self._build_layout()

        self.setWindowFlags(
            Qt.FramelessWindowHint  |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        # NOT WindowTransparentForInput — keyboard needs to receive updates
        # but we drive it from MainController cursor feed anyway
        self.setWindowFlag(Qt.WindowTransparentForInput)
        self.hide()

    def _build_layout(self):
        rows = KB_NUM_ROWS if self._numeric else KB_ROWS
        ks   = KB_KEY_SIZE
        gap  = KB_KEY_GAP

        # Calculate total size
        max_cols = max(len(r) for r in rows)

        # Build key rects — handle special wide keys
        self._keys = []
        y = gap
        for row in rows:
            row_width = sum(self._key_w(k) for k in row) + gap * (len(row) - 1)
            x = gap
            for label in row:
                kw = self._key_w(label)
                self._keys.append((label, QRectF(x, y, kw, ks)))
                x += kw + gap
            y += ks + gap

        total_w = max(
            sum(self._key_w(k) for k in r) + gap * (len(r) - 1) + gap * 2
            for r in rows
        )
        total_h = y + gap

        self.setFixedSize(int(total_w), int(total_h))

        # Create/reset trackers for each unique key
        old = self._trackers
        self._trackers = {}
        for label, _ in self._keys:
            if label in old:
                t = old[label]
                t.reset()
            else:
                t = DwellTracker(DWELL_KEY, once_only=False)
                t.complete.connect(lambda _x, _y, lbl=label: self._on_key_dwell(lbl))
                t.progress.connect(lambda prog, lbl=label: self._on_key_prog(lbl, prog))
                t.cancelled.connect(lambda lbl=label: self._on_key_cancel(lbl))
            self._trackers[label] = t
        self._prog = {k: 0.0 for k, _ in self._keys}

    def _key_w(self, label: str) -> int:
        if label == "Space":
            return KB_KEY_SIZE * 4 + KB_KEY_GAP * 3
        if label in ("ABC", "123", "⏎"):
            return KB_KEY_SIZE * 2 + KB_KEY_GAP
        return KB_KEY_SIZE

    def show_near(self, x: int, y: int):
        scr  = QApplication.primaryScreen().geometry()
        px   = min(x + 20, scr.right()  - self.width()  - 10)
        py   = min(y + 20, scr.bottom() - self.height() - 10)
        px   = max(px, scr.left() + 10)
        py   = max(py, scr.top()  + 10)
        self.move(px, py)
        self.show()
        self.raise_()

    def contains(self, x: int, y: int) -> bool:
        return self.isVisible() and self.geometry().contains(x, y)

    def update_cursor(self, x: int, y: int):
        if not self.isVisible():
            return
        lx = x - self.geometry().x()
        ly = y - self.geometry().y()

        hit = None
        for label, rect in self._keys:
            if rect.contains(lx, ly):
                hit = label
                break

        # Feed tracker for hovered key; cancel others
        for label, tracker in self._trackers.items():
            if label == hit:
                tracker.update(x, y)
            else:
                if self._prog.get(label, 0) > 0:
                    tracker.reset()
                    self._prog[label] = 0.0

        self._hover_key = hit
        self.update()

    def _on_key_prog(self, label: str, prog: float):
        self._prog[label] = prog
        self.update()

    def _on_key_cancel(self, label: str):
        self._prog[label] = 0.0
        self.update()

    def _on_key_dwell(self, label: str):
        self._prog[label] = 0.0
        now = time.monotonic()
        if now - self._last_typed_at < CLICK_COOLDOWN:
            return
        self._last_typed_at = now

        if label == "toggle_num" or label in ("123", "ABC"):
            self._numeric = not self._numeric
            self._build_layout()
            self.update()
            return

        self._type_key(label)
        self.update()

    def _type_key(self, label: str):
        if not PYNPUT_AVAILABLE:
            out = KB_SPECIAL.get(label, label)
            print(f"[Keyboard] TYPE: {out!r}")
            return
        try:
            kb = KbController()
            if label == "⌫":
                kb.press(Key.backspace); kb.release(Key.backspace)
            elif label == "⏎":
                kb.press(Key.enter);     kb.release(Key.enter)
            elif label == "Space":
                kb.press(Key.space);     kb.release(Key.space)
            else:
                kb.type(label)
        except Exception as e:
            print(f"[Keyboard] Error: {e}")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Background
        p.setBrush(QBrush(C_KB_BG))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 10, 10)

        font = QFont("Consolas", 11, QFont.Medium)
        font_sm = QFont("Consolas", 9, QFont.Medium)

        for label, rect in self._keys:
            prog  = self._prog.get(label, 0.0)
            hover = (label == self._hover_key)
            is_sp = label in KB_SPECIAL or label in ("ABC", "123")

            # Key background
            if hover:
                kc = QColor(C_KB_HOVER)
            elif is_sp:
                kc = QColor(C_KB_SPEC)
            else:
                kc = QColor(C_KB_KEY)

            # Highlight fill based on progress
            if prog > 0:
                filled = QColor(C_KB_PROG)
                filled.setAlpha(int(prog * 180))
                p.setBrush(QBrush(kc))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(rect, 6, 6)
                p.setBrush(QBrush(filled))
                p.drawRoundedRect(rect, 6, 6)
            else:
                p.setBrush(QBrush(kc))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(rect, 6, 6)

            # Progress arc on key edge
            if prog > 0:
                cx = rect.center().x()
                cy = rect.center().y()
                rr = min(rect.width(), rect.height()) / 2 - 3
                draw_ring(p, cx, cy, rr, prog, C_KB_PROG, 2.0)

            # Label text
            p.setPen(QPen(C_KB_TEXT))
            p.setFont(font_sm if len(label) > 2 else font)
            p.drawText(rect, Qt.AlignCenter, label)

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# RightPanel — main arm button + scroll circles, always visible
# ─────────────────────────────────────────────────────────────────────────────

class RightPanel(QWidget):
    arm_left   = Signal()
    arm_right  = Signal()
    disarm     = Signal()
    scroll_evt = Signal(int)   # signed ticks

    def __init__(self):
        super().__init__()
        cfg = load_overlay_config()
        self._mr = MAIN_BTN_R
        self._sr = SCROLL_BTN_R

        scr = QApplication.primaryScreen().geometry()
        self.setFixedSize(scr.width(), scr.height())
        self.move(scr.left(), scr.top())

        self._sys_state   = State.IDLE
        self._main_hover  = False
        self._main_prog   = 0.0
        self._main_pulse  = 0.0
        self._main_flash  = 0.0

        self._su_hover    = False
        self._su_prog     = 0.0
        self._su_active   = False
        self._su_held     = 0.0

        self._sd_hover    = False
        self._sd_prog     = 0.0
        self._sd_active   = False
        self._sd_held     = 0.0

        self._main_t = DwellTracker(DWELL_EDGE)
        self._main_t.progress.connect(self._mp)
        self._main_t.complete.connect(self._mc)
        self._main_t.cancelled.connect(self._mcancel)

        self._su_t = DwellTracker(DWELL_SCROLL_LOAD)
        self._su_t.progress.connect(lambda p: self._sp(p, -1))
        self._su_t.complete.connect(lambda x, y: self._sl(-1))
        self._su_t.cancelled.connect(lambda: self._scancel(-1))

        self._sd_t = DwellTracker(DWELL_SCROLL_LOAD)
        self._sd_t.progress.connect(lambda p: self._sp(p, +1))
        self._sd_t.complete.connect(lambda x, y: self._sl(+1))
        self._sd_t.cancelled.connect(lambda: self._scancel(+1))

        self._scroll_dir   = 0
        self._scroll_held  = 0.0
        self._stimer = QTimer()
        self._stimer.setInterval(SCROLL_TICK_MS)
        self._stimer.timeout.connect(self._scroll_tick)
        self._scroll_speed_init = cfg.get("scrollSpeed", SCROLL_SPEED_INIT)

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._place(cfg)

    def _place(self, cfg):
        scr = QApplication.primaryScreen().geometry()
        def_cx = scr.right() - RIGHT_PAD - self._sr - 4
        def_cy = scr.center().y()
        gap = BTN_GAP
        mr = self._mr
        sr = self._sr

        mx = cfg.get("mainBtnX", -1); my = cfg.get("mainBtnY", -1)
        ux = cfg.get("scrollUpX", -1); uy = cfg.get("scrollUpY", -1)
        dx = cfg.get("scrollDownX", -1); dy = cfg.get("scrollDownY", -1)

        if mx == -1 or my == -1: mx, my = def_cx, def_cy
        if ux == -1 or uy == -1: ux, uy = mx, my - mr - gap - sr
        if dx == -1 or dy == -1: dx, dy = mx, my + mr + gap + sr

        self._cx, self._cy = mx, my
        self._su_x, self._su_y = ux, uy
        self._sd_x, self._sd_y = dx, dy

    def contains(self, x, y):
        pts = [(self._cx, self._cy, self._mr),
               (self._su_x, self._su_y, self._sr),
               (self._sd_x, self._sd_y, self._sr)]
        for (px, py, pr) in pts:
            if math.hypot(x - px, y - py) <= pr + 8:
                return True
        return False

    def set_state(self, s):
        self._sys_state = s
        if s not in (State.ARMED_L, State.ARMED_R):
            self._main_prog = 0.0
            self._main_t.reset()
        self.update()

    def notify_click(self):
        self._main_flash = 1.0

    def update_cursor(self, x, y):
        # Main
        dm = math.hypot(x - self._cx, y - self._cy)
        wm = self._main_hover
        self._main_hover = dm < self._mr + 8
        if self._main_hover:
            self._main_t.update(x, y)
        elif wm:
            self._main_t.reset(); self._main_prog = 0.0; self.update()

        # Scroll up
        du = math.hypot(x - self._su_x, y - self._su_y)
        wu = self._su_hover
        self._su_hover = du < self._sr + 8
        if self._su_hover:
            if self._su_active: self._su_held += TIMER_MS / 1000.0
            else:               self._su_t.update(x, y)
        elif wu:
            self._stop_scroll(-1)

        # Scroll down
        dd = math.hypot(x - self._sd_x, y - self._sd_y)
        wd = self._sd_hover
        self._sd_hover = dd < self._sr + 8
        if self._sd_hover:
            if self._sd_active: self._sd_held += TIMER_MS / 1000.0
            else:               self._sd_t.update(x, y)
        elif wd:
            self._stop_scroll(+1)

    def tick(self, delta):
        if self._sys_state in (State.ARMED_L, State.ARMED_R):
            self._main_pulse = (self._main_pulse + delta * 2.4) % (2 * math.pi)
        if self._main_flash > 0:
            self._main_flash = max(0.0, self._main_flash - delta * 5.0)
        self.update()

    # main dwell
    def _mp(self, p): self._main_prog = p; self.update()
    def _mcancel(self): self._main_prog = 0.0; self.update()
    def _mc(self, _x, _y):
        self._main_prog = 0.0
        if self._sys_state == State.ARMED_L:
            self.arm_right.emit()
        elif self._sys_state == State.ARMED_R:
            self.disarm.emit()
        else:
            self.arm_left.emit()

    # scroll
    def _sp(self, p, d):
        if d == -1: self._su_prog = p
        else:       self._sd_prog = p
        self.update()

    def _sl(self, d):
        if d == -1:
            self._su_active = True; self._su_held = 0.0; self._su_prog = 1.0
        else:
            self._sd_active = True; self._sd_held = 0.0; self._sd_prog = 1.0
        self._scroll_dir  = d
        self._scroll_held = 0.0
        self._stimer.start()
        self.update()

    def _scancel(self, d):
        if d == -1: self._su_prog = 0.0
        else:       self._sd_prog = 0.0
        self.update()

    def _stop_scroll(self, d):
        if d == -1:
            self._su_active = False; self._su_held = 0.0
            self._su_prog = 0.0; self._su_t.reset()
        else:
            self._sd_active = False; self._sd_held = 0.0
            self._sd_prog = 0.0; self._sd_t.reset()
        if not self._su_active and not self._sd_active:
            self._stimer.stop(); self._scroll_held = 0.0
        self.update()

    def set_scroll_speed(self, speed):
        self._scroll_speed_init = max(1, min(10, int(speed)))

    def _scroll_tick(self):
        self._scroll_held += SCROLL_TICK_MS / 1000.0
        speed = min(
            self._scroll_speed_init + int(
                (self._scroll_held / SCROLL_ACCEL_TIME) * (SCROLL_SPEED_MAX - self._scroll_speed_init)
            ), SCROLL_SPEED_MAX
        )
        self.scroll_evt.emit(self._scroll_dir * speed)

    def paintEvent(self, _):
        p  = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self._cx, self._cy
        mr, sr = float(self._mr), float(self._sr)

        # Safe zone hint
        for px, py, pr in [(cx, cy, mr),
                           (self._su_x, self._su_y, sr),
                           (self._sd_x, self._sd_y, sr)]:
            p.setBrush(QBrush(C_SAFE_PILL)); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(px, py), pr + 2, pr + 2)

        # ── Main button ───────────────────────────────────────────────────
        s = self._sys_state
        if   s == State.ARMED_R: body_c, ring_c = C_ARMED_R, C_RING_R
        elif s == State.ARMED_L: body_c, ring_c = C_ARMED_L, C_RING_L
        else:                    body_c, ring_c = C_IDLE,    C_HOVER_L

        # pulse glow
        if s in (State.ARMED_L, State.ARMED_R):
            pv  = 0.5 + 0.5 * math.sin(self._main_pulse)
            gr  = mr + 6 + pv * 5
            glo = QRadialGradient(cx, cy, gr)
            gc  = QColor(body_c); gc.setAlpha(int(48 + pv * 52))
            glo.setColorAt(0, gc); glo.setColorAt(1, QColor(0,0,0,0))
            p.setBrush(QBrush(glo)); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), gr, gr)

        # hover glow (idle)
        if self._main_hover and s == State.IDLE:
            hg = QRadialGradient(cx, cy, mr + 12)
            hc = QColor(C_HOVER_L); hc.setAlpha(42)
            hg.setColorAt(0, hc); hg.setColorAt(1, QColor(0,0,0,0))
            p.setBrush(QBrush(hg)); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), mr + 12, mr + 12)

        # flash
        if self._main_flash > 0:
            fc = QColor(255, 255, 255, int(self._main_flash * 88))
            p.setBrush(QBrush(fc)); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), mr + 3, mr + 3)

        draw_dot(p, cx, cy, mr, body_c)

        # icon
        ia  = 192 if (self._main_hover or s != State.IDLE) else 98
        pen = QPen(QColor(255, 255, 255, ia), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        if s not in (State.ARMED_L, State.ARMED_R):
            for dy in (-5, 0, 5):
                p.drawLine(QPointF(cx-6, cy+dy), QPointF(cx+6, cy+dy))
        elif s == State.ARMED_L:
            p.drawLine(QPointF(cx-4, cy-5), QPointF(cx-4, cy+5))
            p.drawLine(QPointF(cx-4, cy+5), QPointF(cx+4, cy+5))
        else:
            p.drawLine(QPointF(cx-3, cy-5), QPointF(cx-3, cy+5))
            p.drawLine(QPointF(cx-3, cy-5), QPointF(cx+3, cy-5))
            p.drawLine(QPointF(cx+3, cy-5), QPointF(cx+3, cy-1))
            p.drawLine(QPointF(cx-3, cy-1), QPointF(cx+3, cy-1))
            p.drawLine(QPointF(cx,   cy-1), QPointF(cx+4, cy+5))

        draw_ring(p, cx, cy, mr + 7, self._main_prog, ring_c, 2.3)

        # ── Scroll circles ────────────────────────────────────────────────
        for (scx, scy, active, held, hover, prog, dt_held) in [
            (self._su_x, self._su_y, self._su_active, self._su_held, self._su_hover, self._su_prog, self._su_held),
            (self._sd_x, self._sd_y, self._sd_active, self._sd_held, self._sd_hover, self._sd_prog, self._sd_held),
        ]:
            is_up = (scy == self._su_y)
            if active:
                ratio = min(held / SCROLL_ACCEL_TIME, 1.0)
                sc = QColor(
                    int(C_SCROLL.red()   + ratio*(C_SCROLL_A.red()   - C_SCROLL.red())),
                    int(C_SCROLL.green() + ratio*(C_SCROLL_A.green() - C_SCROLL.green())),
                    int(C_SCROLL.blue()  + ratio*(C_SCROLL_A.blue()  - C_SCROLL.blue())),
                    200
                )
            else:
                sc = QColor(C_IDLE); sc.setAlpha(128 if hover else 88)

            draw_dot(p, scx, scy, sr, sc)

            ia2 = 198 if hover else 108
            p.setPen(QPen(QColor(255,255,255,ia2), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            aw = 4.5
            if is_up:
                pts = [QPointF(scx-aw, scy+3), QPointF(scx, scy-3), QPointF(scx+aw, scy+3)]
            else:
                pts = [QPointF(scx-aw, scy-3), QPointF(scx, scy+3), QPointF(scx+aw, scy-3)]
            p.drawPolyline(pts)

            if not active:
                draw_ring(p, scx, scy, sr+5, prog, C_SCROLL, 2.0)
            else:
                angle = (time.monotonic() * 4) % (2 * math.pi)
                dx_ = math.cos(angle) * (sr+5)
                dy_ = math.sin(angle) * (sr+5)
                p.setBrush(QBrush(sc)); p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(scx+dx_, scy+dy_), 2.5, 2.5)

        p.end()

    def reload_config(self):
        cfg = load_overlay_config()
        self._scroll_speed_init = max(1, min(10, int(cfg.get("scrollSpeed", SCROLL_SPEED_INIT))))
        self._place(cfg)
        self.update()


# ─────────────────────────────────────────────────────────────────────────────
# LeftPanel — always visible, double-click + drag + keyboard trigger
# ─────────────────────────────────────────────────────────────────────────────

class LeftPanel(QWidget):
    """
    Five freely positioned overlay action icons.
    Double-click, drag, keyboard, copy, and paste.
    """
    double_click = Signal(float, float)
    arm_drag     = Signal()
    kb_toggle    = Signal()
    copy_action  = Signal()
    paste_action = Signal()

    _BTN_DBL   = 0
    _BTN_DRG   = 1
    _BTN_KB    = 2
    _BTN_COPY  = 3
    _BTN_PASTE = 4
    _NUM_BTNS  = 5

    def __init__(self):
        super().__init__()
        cfg  = load_overlay_config()
        self._ar   = cfg.get("overlayIconRadius", ACTION_BTN_R)
        dwell = cfg.get("dwellTime", DWELL_EDGE)

        scr = QApplication.primaryScreen().geometry()
        self.setFixedSize(scr.width(), scr.height())
        self.move(scr.left(), scr.top())

        self._pts = [(20, 20)] * self._NUM_BTNS
        self._load_pts(cfg)

        self._drag_held  = False
        self._kb_open    = False
        self._active_btn = None
        self._pulse      = 0.0
        self._flash      = [0.0] * self._NUM_BTNS

        self._hover = [False] * self._NUM_BTNS
        self._prog  = [0.0]  * self._NUM_BTNS

        self._trackers = [DwellTracker(dwell) for _ in range(self._NUM_BTNS)]

        # Wire signals
        self._trackers[0].complete.connect(lambda x,y: self.double_click.emit(x, y))
        self._trackers[0].progress.connect(lambda v: self._set_prog(0, v))
        self._trackers[0].cancelled.connect(lambda: self._set_prog(0, 0))

        self._trackers[1].complete.connect(lambda x,y: self.arm_drag.emit())
        self._trackers[1].progress.connect(lambda v: self._set_prog(1, v))
        self._trackers[1].cancelled.connect(lambda: self._set_prog(1, 0))

        self._trackers[2].complete.connect(lambda x,y: self.kb_toggle.emit())
        self._trackers[2].progress.connect(lambda v: self._set_prog(2, v))
        self._trackers[2].cancelled.connect(lambda: self._set_prog(2, 0))

        self._trackers[3].complete.connect(lambda x,y: self.copy_action.emit())
        self._trackers[3].progress.connect(lambda v: self._set_prog(3, v))
        self._trackers[3].cancelled.connect(lambda: self._set_prog(3, 0))

        self._trackers[4].complete.connect(lambda x,y: self.paste_action.emit())
        self._trackers[4].progress.connect(lambda v: self._set_prog(4, v))
        self._trackers[4].cancelled.connect(lambda: self._set_prog(4, 0))

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

    def _load_pts(self, cfg):
        def _get(xk, yk, def_y):
            x = cfg.get(xk, 20)
            y = cfg.get(yk, def_y)
            if x == -1: x = 20
            if y == -1: y = def_y
            return x, y

        self._pts = [
            _get("rcIconX", "rcIconY", 300),
            _get("dragIconX", "dragIconY", 380),
            _get("kbIconX", "kbIconY", 460),
            _get("copyIconX", "copyIconY", 540),
            _get("pasteIconX", "pasteIconY", 620)
        ]

    def _set_prog(self, i, v):
        self._prog[i] = v; self.update()

    def set_drag_held(self, held):
        self._drag_held = held; self.update()

    def set_kb_open(self, open_):
        self._kb_open = open_; self.update()

    def set_state(self, state):
        if state in (State.ARMED_DRAG, State.DRAGGING):
            active = self._BTN_DRG
        else:
            active = None
        if active != self._active_btn:
            self._pulse = 0.0
        self._active_btn = active
        self.update()

    def notify_click(self, idx):
        if idx < len(self._flash):
            self._flash[idx] = 1.0
        self.update()

    def contains(self, x, y):
        # We only claim the points within the radius so we don't trap everything
        ar = self._ar
        for (cx, cy) in self._pts:
            if math.hypot(x - cx, y - cy) <= ar + 8:
                return True
        return False

    def update_cursor(self, x, y):
        ar   = self._ar
        for i, (cx, cy) in enumerate(self._pts):
            dist     = math.hypot(x - cx, y - cy)
            was      = self._hover[i]
            self._hover[i] = dist < ar + 8
            if self._hover[i]:
                self._trackers[i].update(x, y)
            elif was:
                self._trackers[i].reset()
                self._prog[i] = 0.0
                self.update()

    def tick(self, delta):
        if self._active_btn is not None:
            self._pulse = (self._pulse + delta * 2.8) % (2 * math.pi)
        for i, flash in enumerate(self._flash):
            if flash > 0:
                self._flash[i] = max(0.0, flash - delta * 5.0)
        self.update()

    def reload_config(self):
        cfg = load_overlay_config()
        new_ar = cfg.get("overlayIconRadius", ACTION_BTN_R)
        new_dwell = cfg.get("dwellTime", DWELL_EDGE)
        changed = False
        
        if new_ar != self._ar:
            self._ar = new_ar
            changed = True
            
        old_pts = list(self._pts)
        self._load_pts(cfg)
        if old_pts != self._pts:
            changed = True
            
        for t in self._trackers:
            if t.dwell_time != new_dwell:
                t.dwell_time = new_dwell
                changed = True
                
        if changed:
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        ar   = float(self._ar)
        font = QFont("Consolas", 8, QFont.Medium)
        p.setFont(font)

        C_RC    = QColor(255, 171, 64, 182)    # amber for right click
        C_DRAG  = QColor(179, 136, 255, 182)   # purple for drag
        C_COPY  = QColor(58, 180, 220, 182)    # cyan for copy
        C_PASTE = QColor(100, 210, 120, 182)   # green for paste

        labels = ["⧉", "↕", "⌨", "📋", "📌"]
        colors = [
            C_RC,
            C_DRAG_HELD if self._drag_held else C_DRAG,
            C_KB_BTN,
            C_COPY,
            C_PASTE,
        ]
        if self._kb_open:
            colors[2] = QColor(100, 200, 100, 210)

        for i, (cx, cy) in enumerate(self._pts):
            hov   = self._hover[i]
            prog  = self._prog[i]
            flash = self._flash[i]
            col   = colors[i]
            sr    = ar

            if hov:
                p.setBrush(QColor(40, 48, 60, 220))
                p.setPen(QPen(col, 2))
                sr = ar * 1.15
            else:
                p.setBrush(QColor(30, 36, 48, 180))
                p.setPen(QPen(QColor(80, 90, 110), 1))

            if flash > 0:
                sr = ar * (1.0 + 0.3 * flash)
                bc = QColor(col)
                bc.setAlpha(int(255 * flash))
                p.setBrush(bc)

            p.drawEllipse(QPointF(cx, cy), sr, sr)

            if prog > 0 and prog < 1.0:
                p.setPen(QPen(col, 3, Qt.SolidLine, Qt.RoundCap))
                p.setBrush(Qt.NoBrush)
                span = int(-prog * 360 * 16)
                p.drawArc(QRectF(cx - sr - 4, cy - sr - 4,
                                 (sr + 4)*2, (sr + 4)*2),
                          90 * 16, span)

            tc = QColor(255, 255, 255) if hov else QColor(180, 190, 210)
            p.setPen(tc)
            p.drawText(QRectF(cx - sr, cy - sr, sr*2, sr*2),
                       Qt.AlignCenter, labels[i])

            if self._active_btn == i and flash == 0:
                p.setPen(QPen(col, 1.5))
                p.setBrush(Qt.NoBrush)
                pulse_r = sr + 4 + math.sin(self._pulse) * 3
                p.drawEllipse(QPointF(cx, cy), pulse_r, pulse_r)

        p.end()

class CursorRing(QWidget):
    def __init__(self):
        super().__init__()
        self._prog  = 0.0
        self._color = QColor(C_RING_L)
        self._flash = 0.0
        self._on    = False
        self._visible_override = True  # False while in safe zone

        size      = (CURSOR_RING_R + 18) * 2
        self._half = size // 2
        self.setFixedSize(size, size)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.hide()

    def arm(self, color=None, reset=True):
        if color is not None:
            self._color = QColor(color)
        if reset:
            self._prog = 0.0
        self._on = True
        self._maybe_show()

    def disarm(self):
        self._on = False; self._prog = 0.0; self.hide()

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def set_safe(self, in_safe: bool):
        """Hide ring while cursor is in a safe zone."""
        self._visible_override = not in_safe
        self._maybe_show()

    def _maybe_show(self):
        if self._on and self._visible_override:
            self.show()
        else:
            self.hide()

    def set_progress(self, p):
        # Only update if we should be drawing
        if self._visible_override:
            self._prog = p; self.update()

    def fire_flash(self):
        self._flash = 1.0; self._maybe_show(); self.update()

    def follow(self, x, y):
        self.move(x - self._half, y - self._half)

    def tick(self, delta):
        if self._flash > 0:
            self._flash = max(0.0, self._flash - delta * 5.5)
            self.update()

    def paintEvent(self, _):
        if not (self._on and self._visible_override) and self._flash <= 0:
            return
        p  = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h   = self.width(), self.height()
        cx, cy = w/2, h/2
        r      = CURSOR_RING_R
        rc     = self._color
        rect   = QRectF(cx-r, cy-r, r*2, r*2)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(C_RING_BG, CURSOR_RING_W, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(rect)
        if self._prog > 0:
            p.setPen(QPen(rc, CURSOR_RING_W, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, 90*16, -int(self._prog * 360 * 16))
        if self._flash > 0:
            fr = r + self._flash * 13
            fa = int(self._flash * 162)
            fc = QColor(rc.red(), rc.green(), rc.blue(), fa)
            p.setPen(QPen(fc, 1.4))
            p.drawEllipse(QRectF(cx-fr, cy-fr, fr*2, fr*2))
        p.end()

# ─────────────────────────────────────────────────────────────────────────────
# MainController
# ─────────────────────────────────────────────────────────────────────────────

class MainController(QObject):
    def __init__(self):
        super().__init__()
        self._state      = State.IDLE
        self._drag_phase = DragPhase.IDLE
        self._mouse      = MouseController() if PYNPUT_AVAILABLE else None
        self._armed_t    = None
        self._user32     = ctypes.windll.user32 if sys.platform == "win32" else None
        self._last_hwnd  = None
        self._last_hover_pos = None
        self._overlay_hwnds = set()

        self._dwell = DwellTracker(DWELL_CLICK)
        self._dwell.progress.connect(self._on_prog)
        self._dwell.complete.connect(self._on_complete)
        self._dwell.cancelled.connect(self._on_cancel)
        self._kb_dwell = DwellTracker(DWELL_KEY, once_only=False)
        self._kb_dwell.progress.connect(self._on_kb_prog)
        self._kb_dwell.complete.connect(self._on_kb_complete)
        self._kb_dwell.cancelled.connect(self._on_kb_cancel)
        self._ring_mode = None

        self._right  = RightPanel()
        self._left   = LeftPanel()
        self._ring   = CursorRing()
        self._kb     = ModernKeyboardManager()

        self._right.arm_left.connect(self._on_arm_left)
        self._right.arm_right.connect(self._on_arm_right)
        self._right.disarm.connect(self._on_disarm)
        self._right.scroll_evt.connect(self._do_scroll)

        self._left.double_click.connect(self._on_double_click)
        self._left.arm_drag.connect(self._on_arm_drag)
        self._left.kb_toggle.connect(self._on_kb_toggle)
        self._left.copy_action.connect(self._on_copy)
        self._left.paste_action.connect(self._on_paste)

        self._right.show()
        self._left.show()
        if self._user32:
            for widget in (self._right, self._left, self._ring):
                try:
                    hwnd = int(widget.winId())
                    if hwnd:
                        self._overlay_hwnds.add(hwnd)
                except Exception:
                    continue

        self._last_t = time.monotonic()
        self._timer  = QTimer()
        self._timer.setInterval(TIMER_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        print("[DwellClick] Ready.")

        # Config reload timer (every 2 s)
        self._cfg_timer = QTimer()
        self._cfg_timer.setInterval(2000)
        self._cfg_timer.timeout.connect(self._reload_config)
        self._cfg_timer.start()

        self._udp = UdpConfigListener()
        self._udp.config_received.connect(self._on_udp_config)
        self._scroll_speed = load_overlay_config().get("scrollSpeed", SCROLL_SPEED_INIT)

    def _tick(self):
        now   = time.monotonic()
        delta = min(now - self._last_t, 0.05)
        self._last_t = now
        x, y = QCursor.pos().x(), QCursor.pos().y()

        # Timeout
        if self._state in (State.ARMED_L, State.ARMED_R, State.ARMED_DRAG, State.DRAGGING):
            if self._armed_t and now - self._armed_t > ARMED_TIMEOUT:
                if self._state == State.DRAGGING:
                    self._do_mouse_up()
                    self._left.set_drag_held(False)
                self._set(State.IDLE)
                return

        kb_hover = self._kb.contains(x, y)
        kb_external = self._kb.uses_external_dwell() and kb_hover
        in_safe = self._right.contains(x, y) or self._left.contains(x, y) or (kb_hover and not kb_external)

        # Update panels
        self._right.update_cursor(x, y)
        self._right.tick(delta)
        self._left.update_cursor(x, y)
        self._left.tick(delta)
        self._kb.update_cursor(x, y)
        self._left.set_kb_open(self._kb.isVisible())

        self._ring.set_safe(in_safe)
        self._ring.follow(x, y)

        if kb_external:
            self._sync_ring_mode("keyboard")
            self._ring.set_color(C_KB_PROG)
            self._kb_dwell.update(x, y)
            self._dwell.reset()
        else:
            if self._kb_dwell._dwell_t is not None:
                self._kb_dwell.reset()
                if self._ring_mode == "keyboard":
                    self._ring.set_progress(0.0)

            if self._state in (State.ARMED_L, State.ARMED_R, State.ARMED_DRAG, State.DRAGGING):
                self._sync_ring_mode("action")
                self._ring.set_color(ring_color_for_state(self._state))
            else:
                self._sync_ring_mode(None)

            if in_safe:
                if self._dwell._dwell_t is not None:
                    self._dwell.reset()
                    self._ring.set_progress(0.0)
            elif self._state in (State.ARMED_L, State.ARMED_R, State.ARMED_DRAG, State.DRAGGING):
                self._dwell.update(x, y)

        self._ring.tick(delta)

    def _store_target_window(self, x: int, y: int):
        if not self._user32:
            return
        try:
            pt = wintypes.POINT()
            pt.x = int(x)
            pt.y = int(y)
            hwnd = self._user32.WindowFromPoint(pt)
            if not hwnd:
                return

            ga_root = 2  # GA_ROOT
            root_hwnd = self._user32.GetAncestor(hwnd, ga_root)
            if not root_hwnd:
                root_hwnd = hwnd
            if root_hwnd in self._overlay_hwnds:
                return

            self._last_hwnd = root_hwnd
            self._last_hover_pos = (pt.x, pt.y)
        except Exception as e:
            print(f"[DwellClick] target capture error: {e}")

    def _set(self, new: State):
        old = self._state; self._state = new
        print(f"[DwellClick] {old.name} → {new.name}")

        self._right.set_state(new)
        self._left.set_state(new)

        if new in (State.ARMED_L, State.ARMED_R, State.ARMED_DRAG):
            self._armed_t = time.monotonic()
            self._dwell.reset()
            self._sync_ring_mode("action")
            self._ring.set_color(ring_color_for_state(new))
        elif new == State.DRAGGING:
            self._armed_t = time.monotonic()
            self._dwell.reset()
            self._sync_ring_mode("action")
            self._ring.set_color(ring_color_for_state(new))
        elif new == State.IDLE:
            self._armed_t = None; self._drag_phase = DragPhase.IDLE
            self._dwell.reset()
            self._left.set_drag_held(False)
            if not self._kb.uses_external_dwell():
                self._sync_ring_mode(None)
        elif new == State.CLICK:
            self._ring.fire_flash()

    def _sync_ring_mode(self, mode):
        if mode == self._ring_mode:
            return
        self._ring_mode = mode
        self._ring.set_progress(0.0)
        if mode is None:
            self._ring.disarm()
        elif mode == "keyboard":
            self._ring.arm(color=C_KB_PROG)
        else:
            color = ring_color_for_state(self._state) or C_RING_L
            self._ring.arm(color=color)

    def _on_arm_left(self):
        if self._state != State.DRAGGING:
            self._set(State.ARMED_L)
    def _on_arm_right(self):
        if self._state == State.ARMED_L:
            self._set(State.ARMED_R)
    def _on_disarm(self):
        self._set(State.IDLE)

    def _on_double_click(self, x, y):
        self._left.notify_click(LeftPanel._BTN_DBL)
        self._do_dbl_click(x, y)

    def _on_arm_drag(self):
        if self._state == State.DRAGGING:
            self._do_mouse_up()
            self._left.set_drag_held(False)
            self._set(State.IDLE)
        elif self._state == State.ARMED_DRAG:
            self._set(State.IDLE)
        else:
            self._set(State.ARMED_DRAG)

    def _on_prog(self, p):
        self._ring.set_progress(p)
    def _on_cancel(self):
        self._ring.set_progress(0.0)

    def _on_kb_prog(self, p):
        self._sync_ring_mode("keyboard")
        self._ring.set_color(C_KB_PROG)
        self._ring.set_progress(p)

    def _on_kb_cancel(self):
        if self._ring_mode == "keyboard":
            self._ring.set_progress(0.0)

    def _on_kb_complete(self, x, y):
        self._sync_ring_mode("keyboard")
        self._do_click(x, y, right=False)
        self._ring.fire_flash()

    def _on_complete(self, x, y):
        if self._state == State.ARMED_L:
            self._set(State.CLICK)
            self._right.notify_click()
            self._do_click(x, y, right=False)
            QTimer.singleShot(int(CLICK_COOLDOWN * 1000), lambda: self._set(State.IDLE))
            return

        if self._state == State.ARMED_R:
            self._set(State.CLICK)
            self._right.notify_click()
            self._do_click(x, y, right=True)
            QTimer.singleShot(int(CLICK_COOLDOWN * 1000), lambda: self._set(State.IDLE))
            return

        if self._state == State.ARMED_DRAG:
            self._drag_phase = DragPhase.HELD
            self._do_mouse_down()
            self._left.set_drag_held(True)
            self._left.notify_click(LeftPanel._BTN_DRG)
            self._set(State.DRAGGING)
            return

        if self._state == State.DRAGGING:
            self._do_mouse_up()
            self._left.set_drag_held(False)
            self._left.notify_click(LeftPanel._BTN_DRG)
            self._drag_phase = DragPhase.IDLE
            self._set(State.IDLE)

    def _on_kb_toggle(self):
        if self._kb.isVisible():
            self._kb.hide()
            self._left.set_kb_open(False)
            self._kb_dwell.reset()
            if self._state == State.IDLE:
                self._sync_ring_mode(None)
        else:
            if self._state == State.DRAGGING:
                self._do_mouse_up()
                self._left.set_drag_held(False)
            x, y = QCursor.pos().x(), QCursor.pos().y()
            self._set(State.IDLE)
            self._kb.show_near(x, y)
            self._left.set_kb_open(True)

    def _do_click(self, x, y, right):
        self._store_target_window(x, y)
        kind = "RIGHT" if right else "LEFT"
        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] {kind} CLICK ({x:.0f},{y:.0f}) [sim]"); return
        try:
            self._mouse.click(Button.right if right else Button.left)
        except Exception as e: print(f"[DwellClick] {e}")

    def _do_dbl_click(self, x, y):
        self._store_target_window(x, y)
        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] DBL-CLICK ({x:.0f},{y:.0f}) [sim]"); return
        try:
            self._mouse.click(Button.left, 2)
        except Exception as e: print(f"[DwellClick] {e}")

    def _do_mouse_down(self):
        x, y = QCursor.pos().x(), QCursor.pos().y()
        self._store_target_window(x, y)
        if not PYNPUT_AVAILABLE or not self._mouse:
            print("[DwellClick] MOUSE DOWN [sim]"); return
        try: self._mouse.press(Button.left)
        except Exception as e: print(f"[DwellClick] {e}")

    def _do_mouse_up(self):
        if not PYNPUT_AVAILABLE or not self._mouse:
            print("[DwellClick] MOUSE UP [sim]"); return
        try: self._mouse.release(Button.left)
        except Exception as e: print(f"[DwellClick] {e}")

    def _do_scroll(self, amount):
        # Prefer scrolling the last real window hovered by the cursor.
        if self._user32 and self._last_hwnd:
            try:
                wm_mousewheel = 0x020A
                delta = int(-amount * 120)
                delta_word = ctypes.c_ushort(delta & 0xFFFF).value
                w_param = delta_word << 16

                if self._last_hover_pos:
                    cx, cy = self._last_hover_pos
                else:
                    rect = wintypes.RECT()
                    if not self._user32.GetWindowRect(self._last_hwnd, ctypes.byref(rect)):
                        raise OSError("GetWindowRect failed")
                    cx = (rect.left + rect.right) // 2
                    cy = (rect.top + rect.bottom) // 2

                x_word = ctypes.c_ushort(cx & 0xFFFF).value
                y_word = ctypes.c_ushort(cy & 0xFFFF).value
                l_param = (y_word << 16) | x_word

                self._user32.PostMessageW(self._last_hwnd, wm_mousewheel, w_param, l_param)
                return
            except Exception as e:
                print(f"[DwellClick] targeted scroll failed: {e}")

        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] SCROLL {amount:+d} [sim]"); return
        try: self._mouse.scroll(0, -amount)
        except Exception as e: print(f"[DwellClick] scroll: {e}")

    # ── Copy / Paste ──────────────────────────────────────────────────────
    def _on_copy(self):
        self._left.notify_click(LeftPanel._BTN_COPY)
        if not PYNPUT_AVAILABLE:
            print("[DwellClick] COPY [sim]"); return
        try:
            kb = KbController()
            kb.press(Key.ctrl); kb.press('c')
            kb.release('c'); kb.release(Key.ctrl)
            print("[DwellClick] Ctrl+C fired")
        except Exception as e:
            print(f"[DwellClick] copy error: {e}")

    def _on_paste(self):
        self._left.notify_click(LeftPanel._BTN_PASTE)
        if not PYNPUT_AVAILABLE:
            print("[DwellClick] PASTE [sim]"); return
        try:
            kb = KbController()
            kb.press(Key.ctrl); kb.press('v')
            kb.release('v'); kb.release(Key.ctrl)
            print("[DwellClick] Ctrl+V fired")
        except Exception as e:
            print(f"[DwellClick] paste error: {e}")

    # ── Live config reload ────────────────────────────────────────────────
    def _reload_config(self):
        cfg = load_overlay_config()
        self._right.reload_config()
        self._left.reload_config()
        
    def _on_udp_config(self, cfg):
        global _cfg_cache
        _cfg_cache.update(cfg)
        self._right.reload_config()
        self._left.reload_config()

class UdpConfigListener(QObject):
    config_received = Signal(dict)
    def __init__(self):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 55555))
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()
        
    def _listen(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
                if data:
                    cfg = json.loads(data.decode('utf-8'))
                    self.config_received.emit(cfg)
            except socket.timeout:
                pass
            except Exception as e:
                print(f"[UDP] Error: {e}")
                time.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print()
    print("  ┌───────────────────────────────────────────────────────┐")
    print("  │  Dwell Click System  v4                               │")
    print("  ├───────────────────────────────────────────────────────┤")
    print("  │  RIGHT EDGE  (safe zone — ring hidden inside)         │")
    print("  │    [▲]  hover → scroll up (accelerates)               │")
    print("  │    [●]  dwell ×1 → arm LEFT click  (green)            │")
    print("  │    [●]  dwell ×2 → arm RIGHT click (amber)            │")
    print("  │    [●]  dwell ×3 → disarm                             │")
    print("  │    [▼]  hover → scroll down (accelerates)             │")
    print("  │                                                       │")
    print("  │  LEFT EDGE  (safe zone, arms on one dwell)            │")
    print("  │    [⧉]  dwell → immediate double-click                │")
    print("  │    [↕]  dwell → arm drag, dwell = hold/release        │")
    print("  │    [⌨]  dwell → open system keyboard toggle           │")
    print("  │                                                       │")
    print("  │  KEYBOARD  (Windows OSK when available)               │")
    print("  │    Hover any key → dwell click is injected on top     │")
    print("  │    Shift, languages, layout switching stay native     │")
    print("  │    Falls back to the built-in keyboard if needed      │")
    print("  └───────────────────────────────────────────────────────┘")
    print()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    ctrl = MainController()  # noqa

    import signal
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    pulse = QTimer(); pulse.start(250); pulse.timeout.connect(lambda: None)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
