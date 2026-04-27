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
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from enum import Enum, auto
from ctypes import wintypes

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore    import Qt, QTimer, QRectF, QPointF, Signal, QObject
from PySide6.QtGui     import (
    QPainter, QColor, QPen, QBrush, QFont,
    QRadialGradient, QCursor
)

# NOTE: modern_keyboard is NOT a separate module — KeyboardManager is defined
# in this file. The import that was here previously has been removed.

try:
    import hid
    HID_AVAILABLE = True
except ImportError:
    hid = None
    HID_AVAILABLE = False
    print("[DwellClick] hidapi not found - HID gestures disabled. pip install hidapi")

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
RIGHT_PAD           = 4      # px panel inset from right edge
LEFT_PAD            = 4      # px panel inset from left edge

CURSOR_RING_R       = 26
CURSOR_RING_W       = 3
TIMER_MS            = 16

# ESP32 composite HID gesture report
HID_VID            = 0xE502
HID_PID            = 0xA111
HID_PRODUCT        = "ESP32 MPU Mouse"
REPORT_ID_GESTURE  = 2
GESTURE_NAMES      = {1: "Flick", 2: "Shake", 3: "DoubleTilt", 4: "Circle"}
GESTURE_AXES       = ["YAW", "PITCH", "ROLL", ""]
GESTURE_TOAST_MS   = 1600

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
    IDLE       = auto()
    ARMED_L    = auto()
    ARMED_R    = auto()
    ARMED_DBL  = auto()
    ARMED_DRAG = auto()
    CLICK      = auto()
    DRAGGING   = auto()


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
    USER32   = ctypes.windll.user32
    WM_CLOSE = 0x0010
else:
    USER32   = None
    WM_CLOSE = 0


def ring_color_for_state(state: State) -> QColor | None:
    if state == State.ARMED_L:
        return QColor(C_RING_L)
    if state == State.ARMED_R:
        return QColor(C_RING_R)
    if state == State.ARMED_DBL:
        return QColor(C_ACTION)
    if state in (State.ARMED_DRAG, State.DRAGGING):
        return QColor(C_DRAG_HELD)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SystemKeyboardBridge — wraps the Windows on-screen keyboard (osk.exe)
# ─────────────────────────────────────────────────────────────────────────────

class SystemKeyboardBridge(QObject):
    """Thin wrapper around the Windows on-screen keyboard (osk.exe)."""

    def __init__(self):
        super().__init__()
        self._exe  = self._resolve_exe()
        self._proc = None

    def _resolve_exe(self):
        if sys.platform != "win32":
            return None
        candidates = [
            shutil.which("osk.exe"),
            os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "System32", "osk.exe"
            ),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    def available(self) -> bool:
        return bool(self._exe)

    def is_visible(self) -> bool:
        """Return True if the OSK window is currently on screen."""
        return self._find_window() != 0

    def uses_external_dwell(self) -> bool:
        return self.is_visible()

    def show_near(self, _x: int, _y: int) -> bool:
        if not self.available():
            return False
        if self.is_visible():
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
        pass   # OSK handles its own input

    def contains(self, x: int, y: int) -> bool:
        rect = self._get_geometry()
        if rect is None:
            return False
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def _get_geometry(self):
        hwnd = self._find_window()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def _find_window(self):
        if not self.available():
            return 0
        hwnd = USER32.FindWindowW("OSKMainClass", None)
        if hwnd and USER32.IsWindowVisible(hwnd):
            return hwnd
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# DwellKeyboard — floating keyboard overlay (fallback when OSK unavailable)
# ─────────────────────────────────────────────────────────────────────────────

class DwellKeyboard(QWidget):
    """
    Floating keyboard overlay driven entirely by the cursor-feed from
    MainController.update_cursor(). No mouse events needed — the widget
    uses WindowTransparentForInput so it never steals focus.
    Each key has its own DwellTracker. Keyboard is its own safe zone.
    """

    closed = Signal()

    def __init__(self):
        super().__init__()
        self._numeric       = False
        self._keys          = []    # list of (label, QRectF)
        self._prog          = {}    # label → progress 0..1
        self._hover_key     = None
        self._trackers      = {}    # label → DwellTracker
        self._last_typed_at = 0.0

        self._build_layout()

        # All flags set together — setWindowFlag() replaces the whole set,
        # so we always use setWindowFlags() with the complete combination.
        self.setWindowFlags(
            Qt.FramelessWindowHint  |
            Qt.WindowStaysOnTopHint |
            Qt.Tool                 |
            Qt.WindowTransparentForInput   # cursor feed is driven externally
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.hide()

    def _build_layout(self):
        rows = KB_NUM_ROWS if self._numeric else KB_ROWS
        ks   = KB_KEY_SIZE
        gap  = KB_KEY_GAP

        self._keys = []
        y = gap
        for row in rows:
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

        # Re-use existing trackers where possible to avoid dropped progress
        old = self._trackers
        self._trackers = {}
        for label, _ in self._keys:
            if label in old:
                t = old[label]
                t.reset()
            else:
                t = DwellTracker(DWELL_KEY, once_only=False)
                t.complete.connect(
                    lambda _x, _y, lbl=label: self._on_key_dwell(lbl)
                )
                t.progress.connect(
                    lambda prog, lbl=label: self._on_key_prog(lbl, prog)
                )
                t.cancelled.connect(
                    lambda lbl=label: self._on_key_cancel(lbl)
                )
            self._trackers[label] = t

        self._prog = {k: 0.0 for k, _ in self._keys}

    def _key_w(self, label: str) -> int:
        if label == "Space":
            return KB_KEY_SIZE * 4 + KB_KEY_GAP * 3
        if label in ("ABC", "123", "⏎"):
            return KB_KEY_SIZE * 2 + KB_KEY_GAP
        return KB_KEY_SIZE

    def show_near(self, x: int, y: int):
        scr = QApplication.primaryScreen().geometry()
        px  = min(x + 20, scr.right()  - self.width()  - 10)
        py  = min(y + 20, scr.bottom() - self.height() - 10)
        px  = max(px, scr.left() + 10)
        py  = max(py, scr.top()  + 10)
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

        if label in ("123", "ABC"):
            self._numeric = not self._numeric
            self._build_layout()
            self.update()
            return

        self._type_key(label)
        self.update()

    def _type_key(self, label: str):
        if not PYNPUT_AVAILABLE:
            print(f"[Keyboard] TYPE: {KB_SPECIAL.get(label, label)!r}")
            return
        try:
            kb = KbController()
            if label == "⌫":
                kb.press(Key.backspace)
                kb.release(Key.backspace)
            elif label == "⏎":
                kb.press(Key.enter)
                kb.release(Key.enter)
            elif label == "Space":
                kb.press(Key.space)
                kb.release(Key.space)
            else:
                kb.type(label)
        except Exception as e:
            print(f"[Keyboard] Error: {e}")

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        p.setBrush(QBrush(C_KB_BG))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 10, 10)

        font    = QFont("Consolas", 11, QFont.Medium)
        font_sm = QFont("Consolas",  9, QFont.Medium)

        for label, rect in self._keys:
            prog  = self._prog.get(label, 0.0)
            hover = (label == self._hover_key)
            is_sp = label in KB_SPECIAL or label in ("ABC", "123")

            if hover:
                kc = QColor(C_KB_HOVER)
            elif is_sp:
                kc = QColor(C_KB_SPEC)
            else:
                kc = QColor(C_KB_KEY)

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

            if prog > 0:
                cx = rect.center().x()
                cy = rect.center().y()
                rr = min(rect.width(), rect.height()) / 2 - 3
                draw_ring(p, cx, cy, rr, prog, C_KB_PROG, 2.0)

            p.setPen(QPen(C_KB_TEXT))
            p.setFont(font_sm if len(label) > 2 else font)
            p.drawText(rect, Qt.AlignCenter, label)

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# KeyboardManager — uses OSK when available, falls back to DwellKeyboard
# ─────────────────────────────────────────────────────────────────────────────

class KeyboardManager(QObject):
    def __init__(self):
        super().__init__()
        self._system   = SystemKeyboardBridge()
        self._fallback = DwellKeyboard()
        self._mode     = "system" if self._system.available() else "fallback"

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
        # OSK handles its own input — nothing to do in system mode

    def isVisible(self) -> bool:
        if self._mode == "system":
            return self._system.is_visible()   # use is_visible(), not isVisible()
        return self._fallback.isVisible()

    def uses_external_dwell(self) -> bool:
        # OSK has its own dwell support; we don't drive it with our ring
        return self._mode == "system" and self._system.is_visible()


# ─────────────────────────────────────────────────────────────────────────────
# RightPanel — main arm button + scroll circles
# ─────────────────────────────────────────────────────────────────────────────

class RightPanel(QWidget):
    arm_left   = Signal()
    arm_right  = Signal()
    disarm     = Signal()
    scroll_evt = Signal(int)   # signed ticks

    def __init__(self):
        super().__init__()
        mr  = MAIN_BTN_R
        sr  = SCROLL_BTN_R
        gap = BTN_GAP
        pw  = (max(mr, sr) + sr + 20) * 2
        ph  = mr * 2 + gap * 2 + sr * 2 + 32
        self.setFixedSize(pw, ph)

        self._cx   = pw / 2
        self._cy   = ph / 2
        self._su_y = self._cy - mr - gap - sr
        self._sd_y = self._cy + mr + gap + sr
        self._mr   = mr
        self._sr   = sr

        self._sys_state  = State.IDLE
        self._main_hover = False
        self._main_prog  = 0.0
        self._main_pulse = 0.0
        self._main_flash = 0.0

        self._su_hover  = False
        self._su_prog   = 0.0
        self._su_active = False
        self._su_held   = 0.0

        self._sd_hover  = False
        self._sd_prog   = 0.0
        self._sd_active = False
        self._sd_held   = 0.0

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

        self._scroll_dir  = 0
        self._scroll_held = 0.0
        self._stimer = QTimer()
        self._stimer.setInterval(SCROLL_TICK_MS)
        self._stimer.timeout.connect(self._scroll_tick)

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._place()

    def _place(self):
        scr = QApplication.primaryScreen().geometry()
        self.move(
            scr.right() - self.width() + RIGHT_PAD + self._sr + 4,
            scr.center().y() - self.height() // 2
        )

    def contains(self, x, y):
        return self.geometry().contains(x, y)

    def set_state(self, s):
        self._sys_state = s
        if s not in (State.ARMED_L, State.ARMED_R):
            self._main_prog = 0.0
            self._main_t.reset()
        self.update()

    def notify_click(self):
        self._main_flash = 1.0

    def update_cursor(self, x, y):
        geo = self.geometry()
        lx, ly = x - geo.x(), y - geo.y()
        cx, cy = self._cx, self._cy

        # Main button
        dm = math.hypot(lx - cx, ly - cy)
        wm = self._main_hover
        self._main_hover = dm < self._mr + 8
        if self._main_hover:
            self._main_t.update(x, y)
        elif wm:
            self._main_t.reset()
            self._main_prog = 0.0
            self.update()

        # Scroll up
        du = math.hypot(lx - cx, ly - self._su_y)
        wu = self._su_hover
        self._su_hover = du < self._sr + 8
        if self._su_hover:
            if self._su_active:
                self._su_held += TIMER_MS / 1000.0
            else:
                self._su_t.update(x, y)
        elif wu:
            self._stop_scroll(-1)

        # Scroll down
        dd = math.hypot(lx - cx, ly - self._sd_y)
        wd = self._sd_hover
        self._sd_hover = dd < self._sr + 8
        if self._sd_hover:
            if self._sd_active:
                self._sd_held += TIMER_MS / 1000.0
            else:
                self._sd_t.update(x, y)
        elif wd:
            self._stop_scroll(+1)

    def tick(self, delta):
        if self._sys_state in (State.ARMED_L, State.ARMED_R):
            self._main_pulse = (self._main_pulse + delta * 2.4) % (2 * math.pi)
        if self._main_flash > 0:
            self._main_flash = max(0.0, self._main_flash - delta * 5.0)
        self.update()

    # ── Main dwell callbacks ───────────────────────────────────────────────
    def _mp(self, p):      self._main_prog = p; self.update()
    def _mcancel(self):    self._main_prog = 0.0; self.update()

    def _mc(self, _x, _y):
        self._main_prog = 0.0
        if self._sys_state == State.ARMED_L:
            self.arm_right.emit()
        elif self._sys_state == State.ARMED_R:
            self.disarm.emit()
        else:
            self.arm_left.emit()

    # ── Scroll callbacks ───────────────────────────────────────────────────
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
            self._su_prog   = 0.0;   self._su_t.reset()
        else:
            self._sd_active = False; self._sd_held = 0.0
            self._sd_prog   = 0.0;   self._sd_t.reset()
        if not self._su_active and not self._sd_active:
            self._stimer.stop()
            self._scroll_held = 0.0
        self.update()

    def _scroll_tick(self):
        self._scroll_held += SCROLL_TICK_MS / 1000.0
        speed = min(
            SCROLL_SPEED_INIT + int(
                (self._scroll_held / SCROLL_ACCEL_TIME)
                * (SCROLL_SPEED_MAX - SCROLL_SPEED_INIT)
            ),
            SCROLL_SPEED_MAX
        )
        self.scroll_evt.emit(self._scroll_dir * speed)

    def paintEvent(self, _):
        p  = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self._cx, self._cy
        mr, sr = float(self._mr), float(self._sr)

        # Safe-zone hint pill
        sh = QRectF(cx - mr - 2, self._su_y - sr - 4,
                    (mr + 2) * 2,
                    self._sd_y - self._su_y + sr * 2 + 8)
        p.setBrush(QBrush(C_SAFE_PILL))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(sh, mr + 2, mr + 2)

        # ── Main button ───────────────────────────────────────────────────
        s = self._sys_state
        if   s == State.ARMED_R: body_c, ring_c = C_ARMED_R, C_RING_R
        elif s == State.ARMED_L: body_c, ring_c = C_ARMED_L, C_RING_L
        else:                    body_c, ring_c = C_IDLE,    C_HOVER_L

        # Pulse glow when armed
        if s in (State.ARMED_L, State.ARMED_R):
            pv  = 0.5 + 0.5 * math.sin(self._main_pulse)
            gr  = mr + 6 + pv * 5
            glo = QRadialGradient(cx, cy, gr)
            gc  = QColor(body_c)
            gc.setAlpha(int(48 + pv * 52))
            glo.setColorAt(0, gc)
            glo.setColorAt(1, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(glo))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), gr, gr)

        # Hover glow (idle)
        if self._main_hover and s == State.IDLE:
            hg = QRadialGradient(cx, cy, mr + 12)
            hc = QColor(C_HOVER_L)
            hc.setAlpha(42)
            hg.setColorAt(0, hc)
            hg.setColorAt(1, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(hg))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), mr + 12, mr + 12)

        # Click flash
        if self._main_flash > 0:
            fc = QColor(255, 255, 255, int(self._main_flash * 88))
            p.setBrush(QBrush(fc))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), mr + 3, mr + 3)

        draw_dot(p, cx, cy, mr, body_c)

        # Icon
        ia  = 192 if (self._main_hover or s != State.IDLE) else 98
        pen = QPen(QColor(255, 255, 255, ia), 2.0,
                   Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if s not in (State.ARMED_L, State.ARMED_R):
            for dy in (-5, 0, 5):
                p.drawLine(QPointF(cx - 6, cy + dy), QPointF(cx + 6, cy + dy))
        elif s == State.ARMED_L:
            p.drawLine(QPointF(cx - 4, cy - 5), QPointF(cx - 4, cy + 5))
            p.drawLine(QPointF(cx - 4, cy + 5), QPointF(cx + 4, cy + 5))
        else:
            p.drawLine(QPointF(cx - 3, cy - 5), QPointF(cx - 3, cy + 5))
            p.drawLine(QPointF(cx - 3, cy - 5), QPointF(cx + 3, cy - 5))
            p.drawLine(QPointF(cx + 3, cy - 5), QPointF(cx + 3, cy - 1))
            p.drawLine(QPointF(cx - 3, cy - 1), QPointF(cx + 3, cy - 1))
            p.drawLine(QPointF(cx,     cy - 1), QPointF(cx + 4, cy + 5))

        draw_ring(p, cx, cy, mr + 7, self._main_prog, ring_c, 2.3)

        # ── Scroll circles ────────────────────────────────────────────────
        for (scy, active, held, hover, prog) in [
            (self._su_y, self._su_active, self._su_held, self._su_hover, self._su_prog),
            (self._sd_y, self._sd_active, self._sd_held, self._sd_hover, self._sd_prog),
        ]:
            is_up = (scy == self._su_y)
            if active:
                ratio = min(held / SCROLL_ACCEL_TIME, 1.0)
                sc = QColor(
                    int(C_SCROLL.red()   + ratio * (C_SCROLL_A.red()   - C_SCROLL.red())),
                    int(C_SCROLL.green() + ratio * (C_SCROLL_A.green() - C_SCROLL.green())),
                    int(C_SCROLL.blue()  + ratio * (C_SCROLL_A.blue()  - C_SCROLL.blue())),
                    200
                )
            else:
                sc = QColor(C_IDLE)
                sc.setAlpha(128 if hover else 88)

            draw_dot(p, cx, scy, sr, sc)

            ia2 = 198 if hover else 108
            p.setPen(QPen(QColor(255, 255, 255, ia2), 1.7,
                          Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            aw = 4.5
            if is_up:
                pts = [QPointF(cx - aw, scy + 3),
                       QPointF(cx,       scy - 3),
                       QPointF(cx + aw,  scy + 3)]
            else:
                pts = [QPointF(cx - aw, scy - 3),
                       QPointF(cx,       scy + 3),
                       QPointF(cx + aw,  scy - 3)]
            p.drawPolyline(pts)

            if not active:
                draw_ring(p, cx, scy, sr + 5, prog, C_SCROLL, 2.0)
            else:
                angle = (time.monotonic() * 4) % (2 * math.pi)
                dx_ = math.cos(angle) * (sr + 5)
                dy_ = math.sin(angle) * (sr + 5)
                p.setBrush(QBrush(sc))
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(cx + dx_, scy + dy_), 2.5, 2.5)

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# LeftPanel — always visible, double-click + drag + keyboard trigger
# ─────────────────────────────────────────────────────────────────────────────

class LeftPanel(QWidget):
    arm_double = Signal()
    arm_drag   = Signal()
    kb_toggle  = Signal()

    _BTN_DBL = 0
    _BTN_DRG = 1
    _BTN_KB  = 2

    def __init__(self):
        super().__init__()
        ar   = ACTION_BTN_R
        gap  = 12
        step = ar * 2 + gap
        pw   = ar * 2 + 28
        ph   = ar * 6 + gap * 2 + 28
        self.setFixedSize(pw, ph)
        self._ar = ar

        self._cx  = pw / 2
        self._bcy = [
            ph / 2 - step,
            ph / 2,
            ph / 2 + step,
        ]

        self._drag_held  = False
        self._kb_open    = False
        self._active_btn = None
        self._pulse      = 0.0
        self._flash      = [0.0, 0.0, 0.0]
        self._hover      = [False, False, False]
        self._prog       = [0.0,   0.0,   0.0]

        self._trackers = [
            DwellTracker(DWELL_EDGE),
            DwellTracker(DWELL_EDGE),
            DwellTracker(DWELL_EDGE),
        ]
        self._trackers[0].complete.connect(lambda x, y: self.arm_double.emit())
        self._trackers[0].progress.connect(lambda v: self._set_prog(0, v))
        self._trackers[0].cancelled.connect(lambda: self._set_prog(0, 0))

        self._trackers[1].complete.connect(lambda x, y: self.arm_drag.emit())
        self._trackers[1].progress.connect(lambda v: self._set_prog(1, v))
        self._trackers[1].cancelled.connect(lambda: self._set_prog(1, 0))

        self._trackers[2].complete.connect(lambda x, y: self.kb_toggle.emit())
        self._trackers[2].progress.connect(lambda v: self._set_prog(2, v))
        self._trackers[2].cancelled.connect(lambda: self._set_prog(2, 0))

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._place()

    def _place(self):
        scr = QApplication.primaryScreen().geometry()
        # Position so the visible portion of the panel touches the left edge.
        # LEFT_PAD is how many pixels of the panel extend off-screen (creates
        # a natural "dock" feel with only the button faces visible).
        self.move(
            scr.left() - LEFT_PAD,
            scr.center().y() - self.height() // 2
        )

    def _set_prog(self, i, v):
        self._prog[i] = v
        self.update()

    def set_drag_held(self, held):
        self._drag_held = held
        self.update()

    def set_kb_open(self, open_):
        self._kb_open = open_
        self.update()

    def set_state(self, state):
        if state == State.ARMED_DBL:
            active = self._BTN_DBL
        elif state in (State.ARMED_DRAG, State.DRAGGING):
            active = self._BTN_DRG
        else:
            active = None
        if active != self._active_btn:
            self._pulse = 0.0
        self._active_btn = active
        self.update()

    def notify_click(self, idx):
        self._flash[idx] = 1.0
        self.update()

    def contains(self, x, y):
        return self.geometry().contains(x, y)

    def update_cursor(self, x, y):
        geo  = self.geometry()
        lx   = x - geo.x()
        ly   = y - geo.y()
        ar   = self._ar
        for i, bcy in enumerate(self._bcy):
            dist = math.hypot(lx - self._cx, ly - bcy)
            was  = self._hover[i]
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

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        cx   = self._cx
        ar   = float(self._ar)
        font = QFont("Consolas", 8, QFont.Medium)
        p.setFont(font)

        labels = ["⧉", "↕", "⌨"]
        colors = [
            C_ACTION,
            C_DRAG_HELD if self._drag_held else C_ACTION,
            QColor(100, 200, 100, 210) if self._kb_open else C_KB_BTN,
        ]

        for i, bcy in enumerate(self._bcy):
            prog   = self._prog[i]
            hover  = self._hover[i]
            active = (i == self._active_btn)

            bc = QColor(colors[i])
            bc.setAlpha(210 if active else (172 if hover else 105))
            draw_dot(p, cx, bcy, ar, bc)

            ia = 200 if (hover or active) else 120
            p.setPen(QPen(QColor(255, 255, 255, ia)))
            p.drawText(
                QRectF(cx - ar, bcy - ar, ar * 2, ar * 2),
                Qt.AlignCenter, labels[i]
            )

            draw_ring(p, cx, bcy, ar + 6, prog, colors[i], 2.0)

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# CursorRing
# ─────────────────────────────────────────────────────────────────────────────

class CursorRing(QWidget):
    def __init__(self):
        super().__init__()
        self._prog             = 0.0
        self._color            = QColor(C_RING_L)
        self._flash            = 0.0
        self._on               = False
        self._visible_override = True   # False while in any safe zone

        size       = (CURSOR_RING_R + 18) * 2
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
        self._on   = False
        self._prog = 0.0
        self.hide()

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

    def set_safe(self, in_safe: bool):
        """Hide the ring while the cursor is inside any safe zone."""
        self._visible_override = not in_safe
        self._maybe_show()

    def _maybe_show(self):
        if self._on and self._visible_override:
            self.show()
        else:
            self.hide()

    def set_progress(self, p):
        if self._visible_override:
            self._prog = p
            self.update()

    def fire_flash(self):
        self._flash = 1.0
        self._maybe_show()
        self.update()

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
        cx, cy = w / 2, h / 2
        r      = CURSOR_RING_R
        rc     = self._color
        rect   = QRectF(cx - r, cy - r, r * 2, r * 2)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(C_RING_BG, CURSOR_RING_W, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(rect)
        if self._prog > 0:
            p.setPen(QPen(rc, CURSOR_RING_W, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, 90 * 16, -int(self._prog * 360 * 16))
        if self._flash > 0:
            fr = r + self._flash * 13
            fa = int(self._flash * 162)
            fc = QColor(rc.red(), rc.green(), rc.blue(), fa)
            p.setPen(QPen(fc, 1.4))
            p.drawEllipse(QRectF(cx - fr, cy - fr, fr * 2, fr * 2))
        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# HID gesture listener thread
# ─────────────────────────────────────────────────────────────────────────────

def decode_hid_gesture(report) -> str | None:
    data = list(report or [])
    if len(data) >= 3 and data[0] == REPORT_ID_GESTURE:
        gesture_id, gesture_data = data[1], data[2]
    elif len(data) >= 2:
        gesture_id, gesture_data = data[0], data[1]
    else:
        return None

    name     = GESTURE_NAMES.get(gesture_id, f"Gesture {gesture_id}")
    axis_idx = gesture_data & 0x03
    axis     = GESTURE_AXES[axis_idx] if axis_idx < len(GESTURE_AXES) else ""
    if gesture_id in (1, 3) and axis:
        axis += "+" if gesture_data & 0x80 else "-"
    return f"{name} {axis}".strip()


class HidGestureListener(QObject):
    gesture = Signal(str)
    status  = Signal(str)

    def __init__(self):
        super().__init__()
        self._stop   = threading.Event()
        self._thread = None
        self._dev    = None

    def start(self):
        if not HID_AVAILABLE:
            self.status.emit("hidapi unavailable")
            return
        self._thread = threading.Thread(
            target=self._run, name="hid-gesture-listener", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._dev:
            try:
                self._dev.close()
            except Exception:
                pass

    def _open_device(self):
        matches   = hid.enumerate(HID_VID, HID_PID)
        preferred = None
        fallback  = None
        for item in matches:
            product = item.get("product_string") or ""
            if HID_PRODUCT.lower() in product.lower():
                if fallback is None:
                    fallback = item
                if item.get("usage_page") == 65280 and item.get("usage") == 1:
                    preferred = item
                    break
        if preferred is None:
            preferred = fallback if fallback is not None else (matches[0] if matches else None)
        if preferred is None:
            raise RuntimeError("ESP32 HID device not found")
        dev = hid.device()
        dev.open_path(preferred["path"])
        dev.set_nonblocking(True)
        return dev

    def _run(self):
        while not self._stop.is_set():
            try:
                self._dev = self._open_device()
                self.status.emit("HID gestures connected")
                while not self._stop.is_set():
                    report = self._dev.read(64)
                    msg    = decode_hid_gesture(report)
                    if msg:
                        self.gesture.emit(msg)
                    time.sleep(0.02)
            except Exception as exc:
                if not self._stop.is_set():
                    self.status.emit(f"HID gestures waiting: {exc}")
                    time.sleep(2.0)
            finally:
                if self._dev:
                    try:
                        self._dev.close()
                    except Exception:
                        pass
                    self._dev = None


# ─────────────────────────────────────────────────────────────────────────────
# Gesture toast notification
# ─────────────────────────────────────────────────────────────────────────────

class GestureToast(QWidget):
    def __init__(self):
        super().__init__()
        self._text   = ""
        self._alpha  = 0.0
        self._shown_t = 0.0
        self.setFixedSize(300, 72)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.hide()

    def show_message(self, text: str):
        self._text    = text
        self._alpha   = 1.0
        self._shown_t = time.monotonic()
        screen = QApplication.primaryScreen()
        if screen:
            g = screen.availableGeometry()
            self.move(g.center().x() - self.width() // 2, g.top() + 42)
        self.show()
        self.update()

    def tick(self):
        if self._alpha <= 0:
            return
        elapsed_ms = (time.monotonic() - self._shown_t) * 1000.0
        if elapsed_ms > GESTURE_TOAST_MS:
            self._alpha = max(0.0, 1.0 - ((elapsed_ms - GESTURE_TOAST_MS) / 450.0))
            if self._alpha <= 0:
                self.hide()
                return
        self.update()

    def paintEvent(self, _):
        if self._alpha <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        a = int(220 * self._alpha)
        p.setBrush(QColor(18, 22, 30, a))
        p.setPen(QPen(QColor(120, 180, 255, int(210 * self._alpha)), 1.2))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 14, 14)
        p.setPen(QColor(235, 240, 255, int(255 * self._alpha)))
        p.setFont(QFont("Segoe UI", 14, QFont.Bold))
        p.drawText(self.rect(), Qt.AlignCenter, self._text)
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
        self._last_hover_pos  = None
        self._overlay_hwnds   = set()

        self._dwell = DwellTracker(DWELL_CLICK)
        self._dwell.progress.connect(self._on_prog)
        self._dwell.complete.connect(self._on_complete)
        self._dwell.cancelled.connect(self._on_cancel)

        self._kb_dwell = DwellTracker(DWELL_KEY, once_only=False)
        self._kb_dwell.progress.connect(self._on_kb_prog)
        self._kb_dwell.complete.connect(self._on_kb_complete)
        self._kb_dwell.cancelled.connect(self._on_kb_cancel)

        self._ring_mode = None

        self._right         = RightPanel()
        self._left          = LeftPanel()
        self._ring          = CursorRing()
        self._kb            = KeyboardManager()   # local class — no external import
        self._gesture_toast = GestureToast()

        self._hid_gestures = HidGestureListener()
        self._hid_gestures.gesture.connect(self._on_hid_gesture)
        self._hid_gestures.status.connect(
            lambda s: print(f"[DwellClick] {s}")
        )
        self._hid_gestures.start()

        self._right.arm_left.connect(self._on_arm_left)
        self._right.arm_right.connect(self._on_arm_right)
        self._right.disarm.connect(self._on_disarm)
        self._right.scroll_evt.connect(self._do_scroll)

        self._left.arm_double.connect(self._on_arm_double)
        self._left.arm_drag.connect(self._on_arm_drag)
        self._left.kb_toggle.connect(self._on_kb_toggle)

        self._right.show()
        self._left.show()

        if self._user32:
            for widget in (self._right, self._left,
                           self._ring, self._gesture_toast):
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

    def _tick(self):
        now   = time.monotonic()
        delta = min(now - self._last_t, 0.05)
        self._last_t = now
        x, y = QCursor.pos().x(), QCursor.pos().y()

        # Auto-disarm timeout
        if self._state in (State.ARMED_L, State.ARMED_R,
                           State.ARMED_DBL, State.ARMED_DRAG, State.DRAGGING):
            if self._armed_t and (now - self._armed_t) > ARMED_TIMEOUT:
                if self._state == State.DRAGGING:
                    self._do_mouse_up()
                    self._left.set_drag_held(False)
                self._set(State.IDLE)
                return

        kb_hover    = self._kb.contains(x, y)
        kb_external = self._kb.uses_external_dwell() and kb_hover
        in_safe     = (self._right.contains(x, y)
                       or self._left.contains(x, y)
                       or (kb_hover and not kb_external))

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

            if self._state in (State.ARMED_L, State.ARMED_R,
                               State.ARMED_DBL, State.ARMED_DRAG, State.DRAGGING):
                self._sync_ring_mode("action")
                self._ring.set_color(ring_color_for_state(self._state))
            else:
                self._sync_ring_mode(None)

            if in_safe:
                if self._dwell._dwell_t is not None:
                    self._dwell.reset()
                    self._ring.set_progress(0.0)
            elif self._state in (State.ARMED_L, State.ARMED_R,
                                 State.ARMED_DBL, State.ARMED_DRAG, State.DRAGGING):
                self._dwell.update(x, y)

        self._ring.tick(delta)
        self._gesture_toast.tick()

    def stop(self):
        self._hid_gestures.stop()

    def _on_hid_gesture(self, text: str):
        print(f"[DwellClick] HID gesture: {text}")
        self._gesture_toast.show_message(text)

    def _store_target_window(self, x: int, y: int):
        if not self._user32:
            return
        try:
            pt    = wintypes.POINT()
            pt.x  = int(x)
            pt.y  = int(y)
            hwnd  = self._user32.WindowFromPoint(pt)
            if not hwnd:
                return
            ga_root   = 2   # GA_ROOT
            root_hwnd = self._user32.GetAncestor(hwnd, ga_root)
            if not root_hwnd:
                root_hwnd = hwnd
            if root_hwnd in self._overlay_hwnds:
                return
            self._last_hwnd      = root_hwnd
            self._last_hover_pos = (pt.x, pt.y)
        except Exception as e:
            print(f"[DwellClick] target capture error: {e}")

    def _set(self, new: State):
        old = self._state
        self._state = new
        print(f"[DwellClick] {old.name} → {new.name}")

        self._right.set_state(new)
        self._left.set_state(new)

        if new in (State.ARMED_L, State.ARMED_R,
                   State.ARMED_DBL, State.ARMED_DRAG, State.DRAGGING):
            self._armed_t = time.monotonic()
            self._dwell.reset()
            self._sync_ring_mode("action")
            self._ring.set_color(ring_color_for_state(new))
        elif new == State.IDLE:
            self._armed_t    = None
            self._drag_phase = DragPhase.IDLE
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

    # ── State transitions ─────────────────────────────────────────────────
    def _on_arm_left(self):
        if self._state != State.DRAGGING:
            self._set(State.ARMED_L)

    def _on_arm_right(self):
        if self._state == State.ARMED_L:
            self._set(State.ARMED_R)

    def _on_disarm(self):
        self._set(State.IDLE)

    def _on_arm_double(self):
        if self._state == State.DRAGGING:
            return
        if self._state == State.ARMED_DBL:
            self._set(State.IDLE)
        else:
            self._set(State.ARMED_DBL)

    def _on_arm_drag(self):
        if self._state == State.DRAGGING:
            self._do_mouse_up()
            self._left.set_drag_held(False)
            self._set(State.IDLE)
        elif self._state == State.ARMED_DRAG:
            self._set(State.IDLE)
        else:
            self._set(State.ARMED_DRAG)

    # ── Dwell callbacks ───────────────────────────────────────────────────
    def _on_prog(self, p):     self._ring.set_progress(p)
    def _on_cancel(self):      self._ring.set_progress(0.0)

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
            QTimer.singleShot(
                int(CLICK_COOLDOWN * 1000),
                lambda: self._set(State.IDLE)
            )
            return

        if self._state == State.ARMED_R:
            self._set(State.CLICK)
            self._right.notify_click()
            self._do_click(x, y, right=True)
            QTimer.singleShot(
                int(CLICK_COOLDOWN * 1000),
                lambda: self._set(State.IDLE)
            )
            return

        if self._state == State.ARMED_DBL:
            self._set(State.CLICK)
            self._left.notify_click(LeftPanel._BTN_DBL)
            self._do_dbl_click(x, y)
            QTimer.singleShot(
                int(CLICK_COOLDOWN * 1000),
                lambda: self._set(State.IDLE)
            )
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

    # ── Mouse actions ─────────────────────────────────────────────────────
    def _do_click(self, x, y, right):
        self._store_target_window(x, y)
        kind = "RIGHT" if right else "LEFT"
        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] {kind} CLICK ({x:.0f},{y:.0f}) [sim]")
            return
        try:
            self._mouse.click(Button.right if right else Button.left)
        except Exception as e:
            print(f"[DwellClick] {e}")

    def _do_dbl_click(self, x, y):
        self._store_target_window(x, y)
        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] DBL-CLICK ({x:.0f},{y:.0f}) [sim]")
            return
        try:
            self._mouse.click(Button.left, 2)
        except Exception as e:
            print(f"[DwellClick] {e}")

    def _do_mouse_down(self):
        x, y = QCursor.pos().x(), QCursor.pos().y()
        self._store_target_window(x, y)
        if not PYNPUT_AVAILABLE or not self._mouse:
            print("[DwellClick] MOUSE DOWN [sim]")
            return
        try:
            self._mouse.press(Button.left)
        except Exception as e:
            print(f"[DwellClick] {e}")

    def _do_mouse_up(self):
        if not PYNPUT_AVAILABLE or not self._mouse:
            print("[DwellClick] MOUSE UP [sim]")
            return
        try:
            self._mouse.release(Button.left)
        except Exception as e:
            print(f"[DwellClick] {e}")

    def _do_scroll(self, amount):
        # Prefer posting WM_MOUSEWHEEL directly to the last real window
        if self._user32 and self._last_hwnd:
            try:
                wm_mousewheel = 0x020A
                delta         = int(-amount * 120)
                delta_word    = ctypes.c_ushort(delta & 0xFFFF).value
                w_param       = delta_word << 16

                if self._last_hover_pos:
                    cx, cy = self._last_hover_pos
                else:
                    rect = wintypes.RECT()
                    if not self._user32.GetWindowRect(
                            self._last_hwnd, ctypes.byref(rect)):
                        raise OSError("GetWindowRect failed")
                    cx = (rect.left + rect.right)  // 2
                    cy = (rect.top  + rect.bottom) // 2

                x_word  = ctypes.c_ushort(cx & 0xFFFF).value
                y_word  = ctypes.c_ushort(cy & 0xFFFF).value
                l_param = (y_word << 16) | x_word

                self._user32.PostMessageW(
                    self._last_hwnd, wm_mousewheel, w_param, l_param
                )
                return
            except Exception as e:
                print(f"[DwellClick] targeted scroll failed: {e}")

        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] SCROLL {amount:+d} [sim]")
            return
        try:
            self._mouse.scroll(0, -amount)
        except Exception as e:
            print(f"[DwellClick] scroll: {e}")


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
    print("  │    [⧉]  dwell → arm double-click, dwell again fires   │")
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

    ctrl = MainController()
    app.aboutToQuit.connect(ctrl.stop)

    import signal
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    pulse = QTimer()
    pulse.start(250)
    pulse.timeout.connect(lambda: None)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
