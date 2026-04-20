"""
Dwell Click System — v3
========================
Accessibility dwell-clicking for air mouse / head tracking.

Install:   pip install PySide6 pynput
Run:       python dwell_click_system.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 RIGHT-EDGE PANEL  (all controls clustered together)
 ┌───────────────┐
 │   ▲  scroll   │  ← small circle, above main button
 │               │
 │  [●] MAIN     │  ← center: arm left/right click
 │               │
 │   ▼  scroll   │  ← small circle, below main button
 └───────────────┘

 LEFT-SIDE PANEL  (on the LEFT edge, vertically centered)
 ┌───────────┐
 │  [⧉] DBL  │  ← double-click button
 │  [↕] DRG  │  ← drag button
 └───────────┘

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 MAIN BUTTON (right edge, center)
   Hover → idle (blue ring charges) → ARMED LEFT (green)
   Hover again  → amber ring charges → ARMED RIGHT (amber)
   Hover again  → disarm

 SAFE ZONE
   The main button area is a SAFE ZONE when ARMED.
   Cursor can rest on the button cluster without triggering
   the cursor dwell ring. Move away to start dwell-clicking.

 SCROLL UP / DOWN  (circles above & below main button)
   Hover → small circle ring fills (0.6s) → scrolling begins
   Scroll continues as long as cursor stays inside — no dwell loop.
   Leaving zone = instant stop. No interference with rest of screen.

 DOUBLE-CLICK  (left edge, top)
   When ARMED: hover → dwell → fires double-click at cursor pos

 DRAG  (left edge, bottom)
   When ARMED: hover → dwell → mouse-down held
   Move cursor to destination → hover DRAG again → mouse-up

 CURSOR RING
   Green = left-click  |  Amber = right-click
   Safe zone suppresses ring (no accidental fire while resting)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import sys
import math
import time
from enum import Enum, auto

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore    import Qt, QTimer, QRectF, QPointF, Signal, QObject
from PySide6.QtGui     import (
    QPainter, QColor, QPen, QBrush, QFont,
    QRadialGradient, QCursor
)

try:
    from pynput.mouse import Button, Controller as MouseController
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    print("[DwellClick] pynput not found — actions simulated.")
    print("             pip install pynput")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

DWELL_EDGE          = 0.75   # s — charge main button (arm)
DWELL_CLICK         = 1.2    # s — fire click/action from cursor position
DWELL_SCROLL_LOAD   = 0.55   # s — fill scroll circle before scrolling starts
DWELL_ACTION        = 0.9    # s — double-click / drag buttons
ARMED_TIMEOUT       = 7.0    # s — auto-disarm

MOVE_THRESHOLD      = 6      # px — stability window
PREDWELL_DELAY      = 0.18   # s  — warmup before dwell timer starts
CLICK_COOLDOWN      = 0.30   # s  — min gap between fires

SCROLL_TICK_MS      = 100    # ms — interval between scroll events
SCROLL_SPEED_INIT   = 2      # scroll units per tick (start)
SCROLL_SPEED_MAX    = 7      # scroll units per tick (max)
SCROLL_ACCEL_TIME   = 2.5    # s  — time to reach max speed

# Layout
MAIN_BTN_R          = 26     # px radius of main button
SCROLL_BTN_R        = 16     # px radius of scroll circles
ACTION_BTN_R        = 18     # px radius of left-panel buttons
BTN_GAP             = 10     # px gap between main btn and scroll circles
PANEL_PAD           = 6      # px from screen edge (right panel)
LEFT_PANEL_PAD      = 6      # px from left screen edge

CURSOR_RING_R       = 26
CURSOR_RING_W       = 3
TIMER_MS            = 16

# ─── Palette ─────────────────────────────────────────────────────────────────
C_IDLE      = QColor(50,  52,  62,  140)
C_HOVER_L   = QColor(55,  140, 255, 190)
C_ARMED_L   = QColor(55,  205, 115, 210)
C_HOVER_R   = QColor(255, 155, 45,  190)
C_ARMED_R   = QColor(235, 125, 38,  210)
C_SCROLL    = QColor(90,  165, 255, 185)
C_SCROLL_A  = QColor(255, 180, 60,  210)   # scroll at full speed
C_ACTION    = QColor(160, 110, 255, 185)   # double-click / drag
C_DRAG_HELD = QColor(255,  90,  90, 210)   # drag while mouse held
C_RING_L    = QColor(60,  210, 120, 255)
C_RING_R    = QColor(240, 130,  40, 255)
C_RING_BG   = QColor(255, 255, 255,  22)
C_SAFE_HINT = QColor(255, 255, 255,  18)   # faint safe-zone indicator


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class State(Enum):
    IDLE    = auto()
    ARMED_L = auto()
    ARMED_R = auto()
    CLICK   = auto()
    DRAGGING = auto()   # mouse button held, waiting for drop dwell


class DragPhase(Enum):
    IDLE     = auto()
    HELD     = auto()   # mouse down, cursor free to move


# ─────────────────────────────────────────────────────────────────────────────
# DwellTracker — generic reusable dwell timer
# ─────────────────────────────────────────────────────────────────────────────

class DwellTracker(QObject):
    """
    Call update(x,y) every tick.
    Emits progress(0→1), complete(x,y), cancelled().
    once_only=True → after one fire it goes silent until reset().
    """
    progress  = Signal(float)
    complete  = Signal(float, float)
    cancelled = Signal()

    def __init__(self, dwell_time: float, once_only: bool = True):
        super().__init__()
        self.dwell_time  = dwell_time
        self.once_only   = once_only
        self._lx = self._ly = 0
        self._stable_t   = None
        self._dwell_t    = None
        self._last_fire  = 0.0
        self._fired      = False

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

def draw_ring(painter: QPainter, cx: float, cy: float, r: float,
              progress: float, fg: QColor, lw: float = 2.5):
    """Draw a background track ring + filled arc for progress 0→1."""
    rect = QRectF(cx - r, cy - r, r * 2, r * 2)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(C_RING_BG, lw, Qt.SolidLine, Qt.RoundCap))
    painter.drawEllipse(rect)
    if progress > 0:
        span = int(progress * 360 * 16)
        painter.setPen(QPen(fg, lw, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(rect, 90 * 16, -span)


def draw_circle(painter: QPainter, cx: float, cy: float, r: float, color: QColor):
    painter.setBrush(QBrush(color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QPointF(cx, cy), r, r)


# ─────────────────────────────────────────────────────────────────────────────
# RightPanel — single window containing main btn + scroll circles
# ─────────────────────────────────────────────────────────────────────────────

class RightPanel(QWidget):
    """
    One transparent window on the right edge holding three hit-zones:

      [▲]  scroll up    — center at (cx, cy - gap - scroll_r - main_r)
      [●]  main button  — center at (cx, cy)
      [▼]  scroll down  — center at (cx, cy + gap + scroll_r + main_r)

    The ENTIRE panel area is a SAFE ZONE — cursor dwell ring is suppressed
    while the cursor is anywhere inside this window.

    Signals:
      arm_left, arm_right, disarm  — from main button
      scroll_start(dir)            — +1 down / -1 up
      scroll_stop()
    """

    arm_left   = Signal()
    arm_right  = Signal()
    disarm     = Signal()
    scroll_evt = Signal(int)   # direction: +1 down / -1 up, emits while active

    def __init__(self):
        super().__init__()

        # Geometry
        self._mr  = MAIN_BTN_R
        self._sr  = SCROLL_BTN_R
        self._gap = BTN_GAP

        panel_w = (self._mr + self._sr + 20) * 2
        panel_h = (self._mr * 2 + self._gap * 2 + self._sr * 2) + 32
        self.setFixedSize(panel_w, panel_h)

        # Centers in local coords
        self._cx   = panel_w / 2
        self._cy   = panel_h / 2
        self._su_y = self._cy - self._mr - self._gap - self._sr   # scroll up
        self._sd_y = self._cy + self._mr + self._gap + self._sr   # scroll down

        # State
        self._sys_state    = State.IDLE
        self._main_hover   = False
        self._main_prog    = 0.0
        self._main_pulse   = 0.0
        self._main_flash   = 0.0

        self._su_hover     = False
        self._su_prog      = 0.0
        self._su_active    = False
        self._su_held      = 0.0

        self._sd_hover     = False
        self._sd_prog      = 0.0
        self._sd_active    = False
        self._sd_held      = 0.0

        # Dwell trackers
        self._main_tracker = DwellTracker(DWELL_EDGE)
        self._main_tracker.progress.connect(self._on_main_prog)
        self._main_tracker.complete.connect(self._on_main_dwell)
        self._main_tracker.cancelled.connect(self._on_main_cancel)

        self._su_tracker   = DwellTracker(DWELL_SCROLL_LOAD)
        self._su_tracker.progress.connect(lambda p: self._on_scroll_prog(p, -1))
        self._su_tracker.complete.connect(lambda x, y: self._on_scroll_load(-1))
        self._su_tracker.cancelled.connect(lambda: self._on_scroll_cancel(-1))

        self._sd_tracker   = DwellTracker(DWELL_SCROLL_LOAD)
        self._sd_tracker.progress.connect(lambda p: self._on_scroll_prog(p, +1))
        self._sd_tracker.complete.connect(lambda x, y: self._on_scroll_load(+1))
        self._sd_tracker.cancelled.connect(lambda: self._on_scroll_cancel(+1))

        # Scroll tick timer
        self._scroll_dir    = 0
        self._scroll_held   = 0.0
        self._scroll_timer  = QTimer()
        self._scroll_timer.setInterval(SCROLL_TICK_MS)
        self._scroll_timer.timeout.connect(self._do_scroll_tick)

        self.setWindowFlags(
            Qt.FramelessWindowHint      |
            Qt.WindowStaysOnTopHint     |
            Qt.Tool                     |
            Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._place()

    def _place(self):
        scr = QApplication.primaryScreen().geometry()
        x   = scr.right() - self.width() + PANEL_PAD + self._sr + 4
        y   = scr.center().y() - self.height() // 2
        self.move(x, y)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_state(self, s: State):
        self._sys_state = s
        if s == State.IDLE:
            self._main_prog = 0.0
            self._main_tracker.reset()
        self.update()

    def notify_click(self):
        self._main_flash = 1.0

    def cursor_in_safe_zone(self, x: int, y: int) -> bool:
        """Returns True if cursor is inside this panel (= safe zone)."""
        return self.geometry().contains(x, y)

    def update_cursor(self, x: int, y: int):
        """Feed raw screen cursor coords every tick."""
        geo  = self.geometry()
        lx   = x - geo.x()
        ly   = y - geo.y()

        # ── Main button hit ───────────────────────────────────────────────
        dist_main = math.hypot(lx - self._cx, ly - self._cy)
        was_main  = self._main_hover
        self._main_hover = dist_main < self._mr + 8

        if self._main_hover:
            self._main_tracker.update(x, y)
        elif was_main:
            self._main_tracker.reset()
            self._main_prog = 0.0
            self.update()

        # ── Scroll up hit ─────────────────────────────────────────────────
        dist_su  = math.hypot(lx - self._cx, ly - self._su_y)
        was_su   = self._su_hover
        self._su_hover = dist_su < self._sr + 8

        if self._su_hover:
            if self._su_active:
                self._su_held += TIMER_MS / 1000.0
            else:
                self._su_tracker.update(x, y)
        else:
            if was_su:
                self._stop_scroll(-1)

        # ── Scroll down hit ───────────────────────────────────────────────
        dist_sd  = math.hypot(lx - self._cx, ly - self._sd_y)
        was_sd   = self._sd_hover
        self._sd_hover = dist_sd < self._sr + 8

        if self._sd_hover:
            if self._sd_active:
                self._sd_held += TIMER_MS / 1000.0
            else:
                self._sd_tracker.update(x, y)
        else:
            if was_sd:
                self._stop_scroll(+1)

    def tick(self, delta: float):
        if self._sys_state in (State.ARMED_L, State.ARMED_R):
            self._main_pulse = (self._main_pulse + delta * 2.4) % (2 * math.pi)
        if self._main_flash > 0:
            self._main_flash = max(0.0, self._main_flash - delta * 5.0)
        self.update()

    # ── Main button dwell callbacks ───────────────────────────────────────────

    def _on_main_prog(self, p: float):
        self._main_prog = p
        self.update()

    def _on_main_cancel(self):
        self._main_prog = 0.0
        self.update()

    def _on_main_dwell(self, _x, _y):
        self._main_prog = 0.0
        if self._sys_state == State.IDLE:
            self.arm_left.emit()
        elif self._sys_state == State.ARMED_L:
            self.arm_right.emit()
        elif self._sys_state == State.ARMED_R:
            self.disarm.emit()

    # ── Scroll callbacks ──────────────────────────────────────────────────────

    def _on_scroll_prog(self, p: float, direction: int):
        if direction == -1:
            self._su_prog = p
        else:
            self._sd_prog = p
        self.update()

    def _on_scroll_load(self, direction: int):
        """Dwell complete → start continuous scrolling."""
        if direction == -1:
            self._su_active = True
            self._su_held   = 0.0
            self._su_prog   = 1.0
        else:
            self._sd_active = True
            self._sd_held   = 0.0
            self._sd_prog   = 1.0
        self._scroll_dir  = direction
        self._scroll_held = 0.0
        self._scroll_timer.start()
        self.update()

    def _on_scroll_cancel(self, direction: int):
        if direction == -1:
            self._su_prog = 0.0
        else:
            self._sd_prog = 0.0
        self.update()

    def _stop_scroll(self, direction: int):
        if direction == -1:
            self._su_active = False
            self._su_held   = 0.0
            self._su_prog   = 0.0
            self._su_tracker.reset()
        else:
            self._sd_active = False
            self._sd_held   = 0.0
            self._sd_prog   = 0.0
            self._sd_tracker.reset()
        # Stop timer only if both inactive
        if not self._su_active and not self._sd_active:
            self._scroll_timer.stop()
            self._scroll_held = 0.0
        self.update()

    def _do_scroll_tick(self):
        self._scroll_held += SCROLL_TICK_MS / 1000.0
        speed = min(
            SCROLL_SPEED_INIT + int(
                (self._scroll_held / SCROLL_ACCEL_TIME) * (SCROLL_SPEED_MAX - SCROLL_SPEED_INIT)
            ),
            SCROLL_SPEED_MAX
        )
        self.scroll_evt.emit(self._scroll_dir * speed)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        cx = self._cx
        cy = self._cy
        mr = float(self._mr)
        sr = float(self._sr)

        # ── Safe zone hint — very faint vertical pill ─────────────────────
        safe_rect = QRectF(cx - mr - 2, self._su_y - sr - 4,
                           (mr + 2) * 2, self._sd_y - self._su_y + sr * 2 + 8)
        p.setBrush(QBrush(C_SAFE_HINT))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(safe_rect, mr + 2, mr + 2)

        # ── Main button ───────────────────────────────────────────────────
        if self._sys_state == State.ARMED_R:
            body_c = C_ARMED_R
            ring_c = C_RING_R
        elif self._sys_state == State.ARMED_L:
            body_c = C_ARMED_L
            ring_c = C_RING_L
        else:
            body_c = C_IDLE
            ring_c = C_HOVER_L

        # Pulse glow
        if self._sys_state in (State.ARMED_L, State.ARMED_R):
            pulse  = 0.5 + 0.5 * math.sin(self._main_pulse)
            gr     = mr + 6 + pulse * 5
            glow   = QRadialGradient(cx, cy, gr)
            gc     = QColor(body_c)
            gc.setAlpha(int(50 + pulse * 55))
            glow.setColorAt(0.0, gc)
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(glow))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), gr, gr)

        # Hover glow (idle only)
        if self._main_hover and self._sys_state == State.IDLE:
            hg = QRadialGradient(cx, cy, mr + 12)
            hc = QColor(C_HOVER_L)
            hc.setAlpha(45)
            hg.setColorAt(0.0, hc)
            hg.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(hg))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), mr + 12, mr + 12)

        # Flash
        if self._main_flash > 0:
            fc = QColor(255, 255, 255, int(self._main_flash * 90))
            p.setBrush(QBrush(fc))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), mr + 3, mr + 3)

        # Body
        draw_circle(p, cx, cy, mr, body_c)

        # Icon
        ia  = 195 if (self._main_hover or self._sys_state != State.IDLE) else 100
        pen = QPen(QColor(255, 255, 255, ia), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if self._sys_state == State.IDLE:
            for dy in (-5, 0, 5):
                p.drawLine(QPointF(cx - 6, cy + dy), QPointF(cx + 6, cy + dy))
        elif self._sys_state == State.ARMED_L:
            p.drawLine(QPointF(cx - 4, cy - 5), QPointF(cx - 4, cy + 5))
            p.drawLine(QPointF(cx - 4, cy + 5), QPointF(cx + 4, cy + 5))
        else:
            p.drawLine(QPointF(cx - 3, cy - 5), QPointF(cx - 3, cy + 5))
            p.drawLine(QPointF(cx - 3, cy - 5), QPointF(cx + 3, cy - 5))
            p.drawLine(QPointF(cx + 3, cy - 5), QPointF(cx + 3, cy - 1))
            p.drawLine(QPointF(cx - 3, cy - 1), QPointF(cx + 3, cy - 1))
            p.drawLine(QPointF(cx,     cy - 1), QPointF(cx + 4, cy + 5))

        # Main dwell ring
        draw_ring(p, cx, cy, mr + 7, self._main_prog, ring_c, 2.4)

        # ── Scroll up circle ──────────────────────────────────────────────
        su_cy   = self._su_y
        # Color shifts toward orange as speed increases
        if self._su_active:
            ratio  = min(self._su_held / SCROLL_ACCEL_TIME, 1.0)
            su_col = QColor(
                int(C_SCROLL.red()   + ratio * (C_SCROLL_A.red()   - C_SCROLL.red())),
                int(C_SCROLL.green() + ratio * (C_SCROLL_A.green() - C_SCROLL.green())),
                int(C_SCROLL.blue()  + ratio * (C_SCROLL_A.blue()  - C_SCROLL.blue())),
                200
            )
        else:
            su_col = QColor(C_IDLE)
            su_col.setAlpha(130 if self._su_hover else 90)

        draw_circle(p, cx, su_cy, sr, su_col)

        # Up arrow
        ia2 = 200 if self._su_hover else 110
        pen2 = QPen(QColor(255, 255, 255, ia2), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen2)
        p.setBrush(Qt.NoBrush)
        aw = 4.5
        p.drawPolyline([QPointF(cx - aw, su_cy + 3),
                        QPointF(cx,      su_cy - 3),
                        QPointF(cx + aw, su_cy + 3)])

        # Scroll up dwell ring
        if not self._su_active:
            draw_ring(p, cx, su_cy, sr + 5, self._su_prog, C_SCROLL, 2.0)
        else:
            # Spinning dot when active to show "live"
            angle = (time.monotonic() * 4) % (2 * math.pi)
            dx    = math.cos(angle) * (sr + 5)
            dy    = math.sin(angle) * (sr + 5)
            p.setBrush(QBrush(su_col))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx + dx, su_cy + dy), 2.5, 2.5)

        # ── Scroll down circle ────────────────────────────────────────────
        sd_cy = self._sd_y
        if self._sd_active:
            ratio  = min(self._sd_held / SCROLL_ACCEL_TIME, 1.0)
            sd_col = QColor(
                int(C_SCROLL.red()   + ratio * (C_SCROLL_A.red()   - C_SCROLL.red())),
                int(C_SCROLL.green() + ratio * (C_SCROLL_A.green() - C_SCROLL.green())),
                int(C_SCROLL.blue()  + ratio * (C_SCROLL_A.blue()  - C_SCROLL.blue())),
                200
            )
        else:
            sd_col = QColor(C_IDLE)
            sd_col.setAlpha(130 if self._sd_hover else 90)

        draw_circle(p, cx, sd_cy, sr, sd_col)

        ia3 = 200 if self._sd_hover else 110
        pen3 = QPen(QColor(255, 255, 255, ia3), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen3)
        p.setBrush(Qt.NoBrush)
        p.drawPolyline([QPointF(cx - aw, sd_cy - 3),
                        QPointF(cx,      sd_cy + 3),
                        QPointF(cx + aw, sd_cy - 3)])

        if not self._sd_active:
            draw_ring(p, cx, sd_cy, sr + 5, self._sd_prog, C_SCROLL, 2.0)
        else:
            angle = (time.monotonic() * 4) % (2 * math.pi)
            dx    = math.cos(angle) * (sr + 5)
            dy    = math.sin(angle) * (sr + 5)
            p.setBrush(QBrush(sd_col))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx + dx, sd_cy + dy), 2.5, 2.5)

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# LeftPanel — double-click + drag buttons on left edge
# ─────────────────────────────────────────────────────────────────────────────

class LeftPanel(QWidget):
    """
    Two small action circles on the LEFT edge, visible only when ARMED.

    [⧉] DOUBLE-CLICK  — dwell → fire double-click at current cursor pos
    [↕] DRAG          — dwell → mouse-down held; dwell again → mouse-up

    Hidden while IDLE to keep screen uncluttered.
    Also acts as a safe zone — cursor resting here won't fire cursor dwell.
    """

    dbl_click  = Signal()
    drag_down  = Signal()
    drag_up    = Signal()

    def __init__(self):
        super().__init__()

        ar         = ACTION_BTN_R
        gap        = 14
        panel_w    = (ar + 10) * 2
        panel_h    = ar * 2 + gap + ar * 2 + 20
        self.setFixedSize(panel_w, panel_h)

        self._cx    = panel_w / 2
        self._dbl_y = panel_h / 2 - ar - gap / 2
        self._drg_y = panel_h / 2 + ar + gap / 2

        self._ar        = ar
        self._visible   = False
        self._drag_held = False    # True = mouse is currently held down

        self._dbl_prog  = 0.0
        self._dbl_hover = False
        self._drg_prog  = 0.0
        self._drg_hover = False

        self._dbl_tracker = DwellTracker(DWELL_ACTION)
        self._dbl_tracker.progress.connect(lambda p: self._set_prog('dbl', p))
        self._dbl_tracker.complete.connect(lambda x, y: self.dbl_click.emit())
        self._dbl_tracker.cancelled.connect(lambda: self._set_prog('dbl', 0))

        self._drg_tracker = DwellTracker(DWELL_ACTION)
        self._drg_tracker.progress.connect(lambda p: self._set_prog('drg', p))
        self._drg_tracker.complete.connect(lambda x, y: self._on_drag_dwell())
        self._drg_tracker.cancelled.connect(lambda: self._set_prog('drg', 0))

        self.setWindowFlags(
            Qt.FramelessWindowHint      |
            Qt.WindowStaysOnTopHint     |
            Qt.Tool                     |
            Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._place()
        self.hide()

    def _place(self):
        scr = QApplication.primaryScreen().geometry()
        x   = scr.left() - LEFT_PANEL_PAD
        y   = scr.center().y() - self.height() // 2
        self.move(x, y)

    def set_armed(self, armed: bool):
        self._visible = armed
        if armed:
            self.show()
        else:
            self.hide()
            self._dbl_prog = 0.0
            self._drg_prog = 0.0
            self._dbl_tracker.reset()
            self._drg_tracker.reset()

    def set_drag_held(self, held: bool):
        self._drag_held = held
        self.update()

    def cursor_in_safe_zone(self, x: int, y: int) -> bool:
        return self.isVisible() and self.geometry().contains(x, y)

    def update_cursor(self, x: int, y: int):
        if not self._visible:
            return
        geo  = self.geometry()
        lx   = x - geo.x()
        ly   = y - geo.y()

        # Double-click button
        dist_d = math.hypot(lx - self._cx, ly - self._dbl_y)
        was_d  = self._dbl_hover
        self._dbl_hover = dist_d < self._ar + 8
        if self._dbl_hover:
            self._dbl_tracker.update(x, y)
        elif was_d:
            self._dbl_tracker.reset()
            self._dbl_prog = 0.0
            self.update()

        # Drag button
        dist_r = math.hypot(lx - self._cx, ly - self._drg_y)
        was_r  = self._drg_hover
        self._drg_hover = dist_r < self._ar + 8
        if self._drg_hover:
            self._drg_tracker.update(x, y)
        elif was_r:
            self._drg_tracker.reset()
            self._drg_prog = 0.0
            self.update()

    def _set_prog(self, which: str, p: float):
        if which == 'dbl':
            self._dbl_prog = p
        else:
            self._drg_prog = p
        self.update()

    def _on_drag_dwell(self):
        self._drg_prog = 0.0
        if self._drag_held:
            self.drag_up.emit()
        else:
            self.drag_down.emit()

    def tick(self, delta: float):
        self.update()

    def paintEvent(self, _):
        if not self._visible:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        cx   = self._cx
        ar   = float(self._ar)

        # ── Double-click button ───────────────────────────────────────────
        dcy   = self._dbl_y
        d_col = QColor(C_ACTION)
        d_col.setAlpha(170 if self._dbl_hover else 100)
        draw_circle(p, cx, dcy, ar, d_col)

        # Icon: two overlapping squares
        ia = 200 if self._dbl_hover else 120
        pen = QPen(QColor(255, 255, 255, ia), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        s = 5
        p.drawRect(QRectF(cx - s - 1.5, dcy - s - 1.5, s * 2 - 1, s * 2 - 1))
        p.drawRect(QRectF(cx - s + 2.5, dcy - s + 2.5, s * 2 - 1, s * 2 - 1))

        draw_ring(p, cx, dcy, ar + 6, self._dbl_prog, C_ACTION, 2.0)

        # ── Drag button ───────────────────────────────────────────────────
        rcy   = self._drg_y
        r_col = QColor(C_DRAG_HELD if self._drag_held else C_ACTION)
        r_col.setAlpha(190 if self._drag_held else (170 if self._drg_hover else 100))
        draw_circle(p, cx, rcy, ar, r_col)

        ia2 = 200 if (self._drg_hover or self._drag_held) else 120
        pen2 = QPen(QColor(255, 255, 255, ia2), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen2)
        p.setBrush(Qt.NoBrush)
        # Drag icon: vertical arrows
        p.drawLine(QPointF(cx, rcy - 6), QPointF(cx, rcy + 6))
        p.drawPolyline([QPointF(cx - 3, rcy - 3), QPointF(cx, rcy - 6), QPointF(cx + 3, rcy - 3)])
        p.drawPolyline([QPointF(cx - 3, rcy + 3), QPointF(cx, rcy + 6), QPointF(cx + 3, rcy + 3)])

        draw_ring(p, cx, rcy, ar + 6, self._drg_prog,
                  C_DRAG_HELD if self._drag_held else C_ACTION, 2.0)

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# CursorRing
# ─────────────────────────────────────────────────────────────────────────────

class CursorRing(QWidget):
    """Transparent window following cursor; draws dwell progress ring."""

    def __init__(self):
        super().__init__()
        self._prog  = 0.0
        self._right = False
        self._flash = 0.0
        self._on    = False

        size = (CURSOR_RING_R + 18) * 2
        self._half = size // 2
        self.setFixedSize(size, size)
        self.setWindowFlags(
            Qt.FramelessWindowHint      |
            Qt.WindowStaysOnTopHint     |
            Qt.Tool                     |
            Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.hide()

    def arm(self, right=False):
        self._right = right
        self._prog  = 0.0
        self._on    = True
        self.show()

    def disarm(self):
        self._on = False
        self._prog = 0.0
        self.hide()

    def set_progress(self, p: float):
        self._prog = p
        self.update()

    def fire_flash(self):
        self._flash = 1.0
        self.update()

    def follow(self, x: int, y: int):
        self.move(x - self._half, y - self._half)

    def tick(self, delta: float):
        if self._flash > 0:
            self._flash = max(0.0, self._flash - delta * 5.5)
            self.update()

    def paintEvent(self, _):
        if not self._on:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h   = self.width(), self.height()
        cx, cy = w / 2, h / 2
        r      = CURSOR_RING_R
        rc     = C_RING_R if self._right else C_RING_L
        rect   = QRectF(cx - r, cy - r, r * 2, r * 2)

        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(C_RING_BG, CURSOR_RING_W, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(rect)

        if self._prog > 0:
            span = int(self._prog * 360 * 16)
            p.setPen(QPen(rc, CURSOR_RING_W, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, 90 * 16, -span)

        if self._flash > 0:
            fr    = r + self._flash * 13
            fa    = int(self._flash * 165)
            fc    = QColor(rc.red(), rc.green(), rc.blue(), fa)
            frect = QRectF(cx - fr, cy - fr, fr * 2, fr * 2)
            p.setPen(QPen(fc, 1.4))
            p.drawEllipse(frect)

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# StatusBadge
# ─────────────────────────────────────────────────────────────────────────────

class StatusBadge(QWidget):
    def __init__(self):
        super().__init__()
        self._text  = ""
        self._color = QColor(70, 72, 80)
        self._alpha = 0.0
        self._tgt   = 0.0

        self.setFixedSize(100, 26)
        self.setWindowFlags(
            Qt.FramelessWindowHint      |
            Qt.WindowStaysOnTopHint     |
            Qt.Tool                     |
            Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        scr = QApplication.primaryScreen().geometry()
        self.move(scr.center().x() - 50, scr.top() + 10)
        self.show()

    def set_state(self, s: State, drag_held: bool = False):
        cfg = {
            State.IDLE:     ("",           QColor(70,  72,  80),   0.0),
            State.ARMED_L:  ("ARMED  L",   QColor(55,  205, 115),  1.0),
            State.ARMED_R:  ("ARMED  R",   QColor(235, 125, 38),   1.0),
            State.CLICK:    ("CLICK",      QColor(255, 215, 55),   1.0),
            State.DRAGGING: ("DRAGGING",   QColor(255, 90,  90),   1.0),
        }
        text, col, tgt = cfg.get(s, ("", QColor(), 0.0))
        if s == State.DRAGGING and drag_held:
            text = "DRAG HELD"
        self._text, self._color, self._tgt = text, col, tgt
        self.update()

    def tick(self, delta: float):
        self._alpha += (self._tgt - self._alpha) * delta * 9
        self._alpha  = max(0.0, min(1.0, self._alpha))
        self.update()

    def paintEvent(self, _):
        if self._alpha < 0.02:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self._alpha)
        w, h = self.width(), self.height()
        p.setBrush(QBrush(QColor(14, 15, 20, 195)))
        p.setPen(QPen(self._color, 1))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        p.setPen(QPen(self._color))
        p.setFont(QFont("Consolas", 9, QFont.Bold))
        p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, self._text)
        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# MainController
# ─────────────────────────────────────────────────────────────────────────────

class MainController(QObject):
    """
    State machine:

      IDLE
        ├─[right panel 1st dwell]─► ARMED_L
        └─[right panel 2nd dwell]─► ARMED_R  (from ARMED_L)

      ARMED_L / ARMED_R
        ├─[cursor dwell, not in safe zone]─► CLICK → IDLE
        ├─[left panel double-click dwell]──► double-click → IDLE
        ├─[left panel drag dwell]──────────► DRAGGING
        └─[right panel 3rd dwell]──────────► IDLE

      DRAGGING
        ├─[left panel drag dwell again]────► drop (mouse-up) → IDLE
        └─[armed timeout]──────────────────► drop → IDLE

    Safe zones (cursor dwell ring suppressed):
      • Right panel (all 3 circles)
      • Left panel (when visible)
    """

    def __init__(self):
        super().__init__()
        self._state      = State.IDLE
        self._drag_phase = DragPhase.IDLE
        self._mouse      = MouseController() if PYNPUT_AVAILABLE else None
        self._armed_t    = None

        # Cursor dwell tracker — suppressed inside safe zones
        self._dwell = DwellTracker(DWELL_CLICK)
        self._dwell.progress.connect(self._on_prog)
        self._dwell.complete.connect(self._on_complete)
        self._dwell.cancelled.connect(self._on_cancel)

        # Widgets
        self._right  = RightPanel()
        self._left   = LeftPanel()
        self._ring   = CursorRing()
        self._badge  = StatusBadge()

        self._right.arm_left.connect(self._on_arm_left)
        self._right.arm_right.connect(self._on_arm_right)
        self._right.disarm.connect(self._on_disarm)
        self._right.scroll_evt.connect(self._do_scroll)

        self._left.dbl_click.connect(self._on_double_click)
        self._left.drag_down.connect(self._on_drag_down)
        self._left.drag_up.connect(self._on_drag_up)

        self._right.show()

        self._last_t = time.monotonic()
        self._timer  = QTimer()
        self._timer.setInterval(TIMER_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        print("[DwellClick] Ready. Hover right-edge button to arm.")

    # ── Tick ──────────────────────────────────────────────────────────────────

    def _tick(self):
        now   = time.monotonic()
        delta = min(now - self._last_t, 0.05)
        self._last_t = now

        x, y = QCursor.pos().x(), QCursor.pos().y()

        # Armed timeout
        if self._state in (State.ARMED_L, State.ARMED_R, State.DRAGGING):
            if self._armed_t and now - self._armed_t > ARMED_TIMEOUT:
                print("[DwellClick] Timeout — disarming.")
                if self._state == State.DRAGGING:
                    self._do_mouse_up()
                self._set(State.IDLE)
                return

        # Safe zone check — suppress cursor dwell if inside any panel
        in_safe = (self._right.cursor_in_safe_zone(x, y) or
                   self._left.cursor_in_safe_zone(x, y))

        # Feed panels
        self._right.update_cursor(x, y)
        self._right.tick(delta)
        self._left.update_cursor(x, y)
        self._left.tick(delta)

        # Feed cursor dwell only when armed AND not in safe zone
        if self._state in (State.ARMED_L, State.ARMED_R, State.DRAGGING):
            if in_safe:
                # Reset dwell silently — cursor is resting
                if self._dwell._dwell_t is not None:
                    self._dwell.reset()
                    self._ring.set_progress(0.0)
            else:
                self._dwell.update(x, y)
            self._ring.follow(x, y)

        self._ring.tick(delta)
        self._badge.tick(delta)

    # ── State management ──────────────────────────────────────────────────────

    def _set(self, new: State):
        old = self._state
        self._state = new
        print(f"[DwellClick] {old.name} → {new.name}")

        armed = new in (State.ARMED_L, State.ARMED_R, State.DRAGGING)
        self._badge.set_state(new, drag_held=(self._drag_phase == DragPhase.HELD))
        self._right.set_state(new)
        self._left.set_armed(armed)

        if new == State.ARMED_L:
            self._armed_t = time.monotonic()
            self._dwell.reset()
            self._ring.arm(right=False)
        elif new == State.ARMED_R:
            self._armed_t = time.monotonic()
            self._dwell.reset()
            self._ring.arm(right=True)
        elif new == State.DRAGGING:
            self._armed_t = time.monotonic()
            self._dwell.reset()
            # Ring stays visible during drag
        elif new == State.IDLE:
            self._armed_t    = None
            self._drag_phase = DragPhase.IDLE
            self._dwell.reset()
            self._ring.disarm()
        elif new == State.CLICK:
            self._ring.fire_flash()
            self._right.notify_click()

    # ── Panel signals ─────────────────────────────────────────────────────────

    def _on_arm_left(self):
        if self._state == State.IDLE:
            self._set(State.ARMED_L)

    def _on_arm_right(self):
        if self._state == State.ARMED_L:
            self._set(State.ARMED_R)

    def _on_disarm(self):
        self._set(State.IDLE)

    # ── Cursor dwell signals ──────────────────────────────────────────────────

    def _on_prog(self, p: float):
        self._ring.set_progress(p)

    def _on_complete(self, x: float, y: float):
        if self._state not in (State.ARMED_L, State.ARMED_R):
            return
        right = (self._state == State.ARMED_R)
        self._set(State.CLICK)
        self._do_click(x, y, right)
        QTimer.singleShot(int(CLICK_COOLDOWN * 1000), lambda: self._set(State.IDLE))

    def _on_cancel(self):
        self._ring.set_progress(0.0)

    # ── Left panel actions ────────────────────────────────────────────────────

    def _on_double_click(self):
        if self._state not in (State.ARMED_L, State.ARMED_R):
            return
        x, y = QCursor.pos().x(), QCursor.pos().y()
        self._set(State.CLICK)
        self._do_dbl_click(x, y)
        QTimer.singleShot(int(CLICK_COOLDOWN * 1000), lambda: self._set(State.IDLE))

    def _on_drag_down(self):
        if self._state not in (State.ARMED_L, State.ARMED_R):
            return
        self._drag_phase = DragPhase.HELD
        self._set(State.DRAGGING)
        self._do_mouse_down()
        self._badge.set_state(State.DRAGGING, drag_held=True)
        self._left.set_drag_held(True)

    def _on_drag_up(self):
        if self._state != State.DRAGGING:
            return
        self._do_mouse_up()
        self._left.set_drag_held(False)
        self._set(State.IDLE)

    # ── Mouse actions ─────────────────────────────────────────────────────────

    def _do_click(self, x: float, y: float, right: bool):
        kind = "RIGHT" if right else "LEFT"
        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] {kind} CLICK ({x:.0f},{y:.0f}) [sim]")
            return
        try:
            self._mouse.click(Button.right if right else Button.left)
            print(f"[DwellClick] {kind} CLICK ({x:.0f},{y:.0f})")
        except Exception as e:
            print(f"[DwellClick] Error: {e}")

    def _do_dbl_click(self, x: float, y: float):
        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] DOUBLE-CLICK ({x:.0f},{y:.0f}) [sim]")
            return
        try:
            self._mouse.click(Button.left, 2)
            print(f"[DwellClick] DOUBLE-CLICK ({x:.0f},{y:.0f})")
        except Exception as e:
            print(f"[DwellClick] Error: {e}")

    def _do_mouse_down(self):
        if not PYNPUT_AVAILABLE or not self._mouse:
            print("[DwellClick] MOUSE DOWN [sim]")
            return
        try:
            self._mouse.press(Button.left)
            print("[DwellClick] MOUSE DOWN")
        except Exception as e:
            print(f"[DwellClick] Error: {e}")

    def _do_mouse_up(self):
        if not PYNPUT_AVAILABLE or not self._mouse:
            print("[DwellClick] MOUSE UP [sim]")
            return
        try:
            self._mouse.release(Button.left)
            print("[DwellClick] MOUSE UP")
        except Exception as e:
            print(f"[DwellClick] Error: {e}")

    def _do_scroll(self, amount: int):
        """amount = signed scroll ticks (+ve = down, -ve = up)."""
        if not PYNPUT_AVAILABLE or not self._mouse:
            print(f"[DwellClick] SCROLL {amount:+d} [sim]")
            return
        try:
            # pynput scroll: dy positive = up, negative = down
            self._mouse.scroll(0, -amount)
        except Exception as e:
            print(f"[DwellClick] Scroll error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print()
    print("  ┌──────────────────────────────────────────────────────┐")
    print("  │  Dwell Click System  v3                              │")
    print("  ├──────────────────────────────────────────────────────┤")
    print("  │  RIGHT EDGE (all clustered — also a safe rest zone)  │")
    print("  │    [▲]  hover → scroll up (accelerates)              │")
    print("  │    [●]  1st dwell → arm LEFT  (green)                │")
    print("  │    [●]  2nd dwell → arm RIGHT (amber)                │")
    print("  │    [●]  3rd dwell → disarm                           │")
    print("  │    [▼]  hover → scroll down (accelerates)            │")
    print("  │                                                      │")
    print("  │  LEFT EDGE (appears when armed)                      │")
    print("  │    [⧉]  dwell → double-click at cursor               │")
    print("  │    [↕]  dwell → hold drag / dwell again to drop      │")
    print("  │                                                      │")
    print("  │  Cursor ring fills → click fires                     │")
    print("  │  Resting on either panel = safe (no ring fill)       │")
    print("  └──────────────────────────────────────────────────────┘")
    print()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    ctrl = MainController()  # noqa

    import signal
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    sig_t = QTimer()
    sig_t.start(250)
    sig_t.timeout.connect(lambda: None)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()