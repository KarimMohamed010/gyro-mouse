"""
Modern on-screen keyboard for assistive input systems.

The module exposes a cross-platform keyboard widget and a thin manager class
compatible with overlay integration points:
- show_near(x, y)
- hide()
- contains(x, y)
- update_cursor(x, y)
- isVisible()
- uses_external_dwell()

Design goals:
- Accessibility first: dwell input, scanning mode, external control hooks
- Mobile-like visual quality: rounded keys, suggestion strip, smooth feedback
- Modular architecture: input injection, prediction, UI, plugin hooks
- Offline-by-default privacy model with optional persistence
"""

from __future__ import annotations

import difflib
import json
import math
import os
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


# Public color used by overlay ring integration.
C_KB_PROG = QColor(100, 160, 255, 255)


@dataclass
class KeyboardTheme:
    name: str
    bg: QColor
    key: QColor
    key_hover: QColor
    key_hold: QColor
    key_pressed: QColor
    special: QColor
    text: QColor
    hint: QColor
    shadow: QColor


THEMES: Dict[str, KeyboardTheme] = {
    "dark": KeyboardTheme(
        name="dark",
        bg=QColor(20, 24, 32, 235),
        key=QColor(52, 58, 72, 240),
        key_hover=QColor(74, 96, 148, 250),
        key_hold=QColor(95, 143, 225, 255),
        key_pressed=QColor(90, 178, 130, 245),
        special=QColor(40, 46, 60, 245),
        text=QColor(235, 236, 242, 255),
        hint=QColor(184, 188, 199, 255),
        shadow=QColor(10, 12, 18, 160),
    ),
    "light": KeyboardTheme(
        name="light",
        bg=QColor(242, 245, 250, 245),
        key=QColor(255, 255, 255, 250),
        key_hover=QColor(213, 227, 249, 255),
        key_hold=QColor(149, 188, 242, 255),
        key_pressed=QColor(149, 219, 182, 255),
        special=QColor(233, 237, 244, 255),
        text=QColor(32, 36, 45, 255),
        hint=QColor(90, 98, 112, 255),
        shadow=QColor(130, 138, 154, 110),
    ),
    "high_contrast": KeyboardTheme(
        name="high_contrast",
        bg=QColor(0, 0, 0, 242),
        key=QColor(28, 28, 28, 255),
        key_hover=QColor(0, 93, 186, 255),
        key_hold=QColor(255, 170, 0, 255),
        key_pressed=QColor(0, 160, 90, 255),
        special=QColor(8, 8, 8, 255),
        text=QColor(255, 255, 255, 255),
        hint=QColor(214, 214, 214, 255),
        shadow=QColor(0, 0, 0, 180),
    ),
}


@dataclass
class KeyboardConfig:
    dwell_time: float = 0.7
    move_threshold: float = 6.0
    predwell_delay: float = 0.16
    cooldown: float = 0.28
    key_size: int = 50
    key_gap: int = 6
    scale: float = 1.0
    suggestion_count: int = 0
    autocomplete_enabled: bool = False
    autocorrect_level: float = 0.55
    scanning_interval_ms: int = 1200
    scan_column_interval_ms: int = 800
    swipe_link_window: float = 0.20
    swipe_commit_delay: float = 0.32


@dataclass
class KeySpec:
    key_id: str
    label: str
    kind: str = "char"
    width_units: float = 1.0
    payload: Optional[str] = None


class KeyboardPlugin(Protocol):
    def on_registered(self, keyboard: "ModernOnScreenKeyboard") -> None:
        ...

    def on_key_hover(self, key_meta: dict) -> None:
        ...

    def on_key_press(self, key_meta: dict) -> None:
        ...

    def on_key_hold(self, key_meta: dict) -> None:
        ...


class BaseInputInjector:
    def type_text(self, text: str) -> None:
        raise NotImplementedError

    def tap(self, key_name: str) -> None:
        raise NotImplementedError

    def chord(self, modifiers: Sequence[str], key_name: str) -> None:
        raise NotImplementedError

    def press_modifier(self, modifier: str, down: bool) -> None:
        raise NotImplementedError

    def backspaces(self, count: int) -> None:
        for _ in range(max(0, count)):
            self.tap("backspace")


class NullInputInjector(BaseInputInjector):
    def type_text(self, text: str) -> None:
        print(f"[ModernKeyboard] TYPE {text!r}")

    def tap(self, key_name: str) -> None:
        print(f"[ModernKeyboard] TAP {key_name}")

    def chord(self, modifiers: Sequence[str], key_name: str) -> None:
        print(f"[ModernKeyboard] CHORD {list(modifiers)} + {key_name}")

    def press_modifier(self, modifier: str, down: bool) -> None:
        print(f"[ModernKeyboard] MOD {modifier} {'down' if down else 'up'}")


class PynputInputInjector(BaseInputInjector):
    def __init__(self):
        from pynput.keyboard import Controller, Key

        self._controller = Controller()
        self._Key = Key
        self._held_modifiers: Dict[str, object] = {}

    def _map_key(self, key_name: str):
        alias = {
            "enter": self._Key.enter,
            "backspace": self._Key.backspace,
            "space": self._Key.space,
            "tab": self._Key.tab,
            "esc": self._Key.esc,
            "delete": self._Key.delete,
            "left": self._Key.left,
            "right": self._Key.right,
            "up": self._Key.up,
            "down": self._Key.down,
            "home": self._Key.home,
            "end": self._Key.end,
            "pageup": self._Key.page_up,
            "pagedown": self._Key.page_down,
            "shift": self._Key.shift,
            "ctrl": self._Key.ctrl,
            "alt": self._Key.alt,
            "cmd": self._Key.cmd,
            "f1": self._Key.f1,
            "f2": self._Key.f2,
            "f3": self._Key.f3,
            "f4": self._Key.f4,
            "f5": self._Key.f5,
            "f6": self._Key.f6,
            "f7": self._Key.f7,
            "f8": self._Key.f8,
            "f9": self._Key.f9,
            "f10": self._Key.f10,
            "f11": self._Key.f11,
            "f12": self._Key.f12,
        }
        return alias.get(key_name.lower(), key_name)

    def type_text(self, text: str) -> None:
        self._controller.type(text)

    def tap(self, key_name: str) -> None:
        mapped = self._map_key(key_name)
        self._controller.press(mapped)
        self._controller.release(mapped)

    def chord(self, modifiers: Sequence[str], key_name: str) -> None:
        pressed = []
        try:
            for modifier in modifiers:
                mk = self._map_key(modifier)
                self._controller.press(mk)
                pressed.append(mk)
            kk = self._map_key(key_name)
            self._controller.press(kk)
            self._controller.release(kk)
        finally:
            for modifier in reversed(pressed):
                self._controller.release(modifier)

    def press_modifier(self, modifier: str, down: bool) -> None:
        mapped = self._map_key(modifier)
        held = modifier in self._held_modifiers
        if down and not held:
            self._controller.press(mapped)
            self._held_modifiers[modifier] = mapped
        elif (not down) and held:
            self._controller.release(self._held_modifiers[modifier])
            del self._held_modifiers[modifier]


class OfflinePredictionEngine:
    """Small offline prediction engine with user learning persistence."""

    def __init__(self, language: str = "en"):
        self.language = language
        self._base = self._base_lexicon(language)
        self._user_freq: Counter[str] = Counter()
        self._next_word: Dict[Tuple[str, str], int] = {}
        self._load_user_lexicon()

    def set_language(self, language: str) -> None:
        if language == self.language:
            return
        self.language = language
        self._base = self._base_lexicon(language)

    def suggest(self, prefix: str, last_word: str, limit: int = 3) -> List[str]:
        try:
            return self._suggest_impl(prefix, last_word, limit)
        except Exception as exc:
            print(f"[ModernKeyboard] Prediction fallback: {exc}")
            return []

    def _suggest_impl(self, prefix: str, last_word: str, limit: int) -> List[str]:
        prefix = prefix.lower().strip()
        last_word = last_word.lower().strip()

        pool = Counter(self._base)
        pool.update(self._user_freq)

        results: List[Tuple[str, float]] = []
        for word, score in pool.items():
            if prefix and not word.startswith(prefix):
                continue
            rank = float(score)
            if last_word:
                rank += self._next_word.get((last_word, word), 0) * 0.9
            if prefix and word == prefix:
                rank *= 0.8
            results.append((word, rank))

        results.sort(key=lambda item: item[1], reverse=True)
        return [word for word, _ in results[:limit]]

    def autocorrect(self, word: str, aggressiveness: float) -> str:
        w = word.strip()
        if not w:
            return w
        lower = w.lower()
        pool = set(self._base.keys()) | set(self._user_freq.keys())
        if lower in pool:
            return w
        # Low aggressiveness only fixes near-certain typos.
        cutoff = 0.93 - min(max(aggressiveness, 0.0), 1.0) * 0.18
        candidates = difflib.get_close_matches(lower, list(pool), n=1, cutoff=cutoff)
        if not candidates:
            return w
        corrected = candidates[0]
        if w.istitle():
            corrected = corrected.title()
        elif w.isupper():
            corrected = corrected.upper()
        return corrected

    def learn_word(self, word: str, prev_word: str = "") -> None:
        cleaned = "".join(ch for ch in word.lower() if ch.isalpha() or ch in "-'")
        if len(cleaned) < 2:
            return
        self._user_freq[cleaned] += 1
        prev = prev_word.lower().strip()
        if prev:
            key = (prev, cleaned)
            self._next_word[key] = self._next_word.get(key, 0) + 1
        self._save_user_lexicon()

    def decode_swipe(self, letters: Sequence[str]) -> Optional[str]:
        if len(letters) < 3:
            return None
        normalized = [ch.lower() for ch in letters if ch and ch.isalpha()]
        if len(normalized) < 3:
            return None

        start = normalized[0]
        end = normalized[-1]
        pool = Counter(self._base)
        pool.update(self._user_freq)

        best_word = None
        best_score = 0.0
        path = "".join(normalized)

        for word, freq in pool.items():
            if len(word) < 3 or word[0] != start or word[-1] != end:
                continue
            score = float(freq)
            if self._is_subsequence(path, word):
                score += 2.0
            if path[0] == word[0] and path[-1] == word[-1]:
                score += 0.6
            score -= abs(len(word) - len(path)) * 0.12
            if score > best_score:
                best_score = score
                best_word = word
        return best_word

    @staticmethod
    def _is_subsequence(path: str, word: str) -> bool:
        it = iter(word)
        return all(ch in it for ch in path)

    def _user_file(self) -> Path:
        root = Path.home() / ".dwell_keyboard"
        root.mkdir(parents=True, exist_ok=True)
        return root / "user_vocab.json"

    def _load_user_lexicon(self) -> None:
        path = self._user_file()
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._user_freq.update(payload.get("freq", {}))
            raw_next = payload.get("next", {})
            for pair, count in raw_next.items():
                parts = pair.split("\t", 1)
                if len(parts) == 2:
                    self._next_word[(parts[0], parts[1])] = int(count)
        except Exception as exc:
            print(f"[ModernKeyboard] Unable to load learned vocab: {exc}")

    def _save_user_lexicon(self) -> None:
        try:
            payload = {
                "freq": dict(self._user_freq),
                "next": {f"{a}\t{b}": c for (a, b), c in self._next_word.items()},
            }
            self._user_file().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f"[ModernKeyboard] Unable to persist learned vocab: {exc}")

    @staticmethod
    def _base_lexicon(language: str) -> Counter[str]:
        common_en = """
        the be to of and a in that have i it for not on with he as you do at this but
        his by from they we say her she or an will my one all would there their what
        so up out if about who get which go me when make can like time no just him know
        take people into year your good some could them see other than then now look only
        come its over think also back after use two how our work first well way even new
        want because any these give day most us hello yes no keyboard typing input dwell
        assistive accessibility sensor gesture swipe predict autocorrect model offline cloud
        """.split()

        common_es = """
        de la que el en y a los del se las por un para con no una su al lo como mas pero
        sus le ya o este si porque esta entre cuando muy sin sobre tambien me hasta hay
        donde quien desde todo nos durante todos uno les ni contra otros ese eso ante ellos
        e esto mi antes algunos que yo otro otras otra el mismo esta vez bien despues
        hola teclado escribir accesibilidad asistencia palabra sugerencia
        """.split()

        common_fr = """
        de la le et les des en un une pour que dans qui sur pas plus ne au ce il elle
        nous vous ils elles avec comme mais ou donc si est sont avoir etre fait bien
        clavier accessibilite saisie prediction correction bonjour merci
        """.split()

        if language == "es":
            corpus = common_es
        elif language == "fr":
            corpus = common_fr
        else:
            corpus = common_en
        return Counter(corpus)


class ClipboardHistory(QObject):
    changed = Signal(list)

    def __init__(self, limit: int = 20):
        super().__init__()
        self._limit = limit
        self._items: deque[str] = deque(maxlen=limit)
        clipboard = QApplication.clipboard()
        clipboard.dataChanged.connect(self._on_clipboard_changed)

    def _on_clipboard_changed(self) -> None:
        text = QApplication.clipboard().text(mode=QApplication.clipboard().Clipboard)
        text = text.strip()
        if not text:
            return
        if self._items and self._items[0] == text:
            return
        self._items.appendleft(text)
        self.changed.emit(list(self._items))

    def items(self) -> List[str]:
        return list(self._items)


class ModernOnScreenKeyboard(QWidget):
    """Cross-platform keyboard widget intended for dwell/head/eye tracking."""

    onKeyHover = Signal(dict)
    onKeyPress = Signal(dict)
    onKeyHold = Signal(dict)
    suggestionsChanged = Signal(list)

    def __init__(self, config: Optional[KeyboardConfig] = None):
        super().__init__()

        self.config = config or KeyboardConfig()
        self._theme = THEMES["dark"]
        self._language_cycle = ["en", "es", "fr"]
        self._language = "en"
        self._keyboard_mode = "floating"  # floating, docked, split
        self._layout_layer = "letters"  # letters, numbers, symbols, functions, emoji, settings
        self._emoji_page = 0
        self._clipboard_panel = False
        self._snap_cycle = ["cursor", "top_left", "top_right", "bottom_left", "bottom_right", "bottom_center"]
        self._snap_index = 0
        self._snap_anchor = self._snap_cycle[self._snap_index]

        self._shift_once = False
        self._caps_lock = False
        self._ctrl = False
        self._alt = False

        self._predictor = OfflinePredictionEngine(language=self._language)
        self._last_word = ""
        self._current_word = ""
        self._suggestions: List[str] = []

        self._plugins: List[KeyboardPlugin] = []
        self._audio_hook: Optional[Callable[[str], None]] = None
        self._haptic_hook: Optional[Callable[[str], None]] = None
        self._tts_hook: Optional[Callable[[str], None]] = None

        self._input = self._build_input_injector()
        self._clipboard = ClipboardHistory()
        self._clipboard.changed.connect(self._on_clipboard_history_changed)

        self._key_rects: Dict[str, QRectF] = {}
        self._key_specs: Dict[str, KeySpec] = {}
        self._clip_item_ids: List[str] = []
        self._progress: Dict[str, float] = {}
        self._pressed_flash: Dict[str, float] = {}
        self._hover_key: Optional[str] = None
        self._hover_hold_emitted = False

        self._hover_started_at: Optional[float] = None
        self._hover_stable_since: Optional[float] = None
        self._hover_last_pos: Optional[Tuple[float, float]] = None
        self._last_fire_at = 0.0

        self._swipe_enabled = True
        self._swipe_letters: List[str] = []
        self._swipe_last_t = 0.0

        self._scanning_enabled = False
        self._scanning_row_mode = True
        self._scan_row = 0
        self._scan_col = 0
        self._scan_rows: List[List[str]] = []
        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._scan_advance)

        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._on_anim)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowFlag(Qt.WindowTransparentForInput)

        self._rebuild_layout()
        self.hide()

    # ------------------------------ Public API ------------------------------

    def show_near(self, x: int, y: int) -> None:
        self._place_window(x, y)
        self.show()
        self.raise_()
        self._anim.start()

    def hide(self) -> None:  # type: ignore[override]
        super().hide()
        self._anim.stop()

    def contains(self, x: int, y: int) -> bool:
        return self.isVisible() and self.geometry().contains(x, y)

    def update_cursor(self, x: int, y: int) -> None:
        if not self.isVisible():
            return
        now = time.monotonic()

        key_id = self._hit_test_global(x, y)

        if key_id is None and self._swipe_enabled:
            if self._swipe_letters and (now - self._swipe_last_t) > self.config.swipe_commit_delay:
                self._commit_swipe_path()

        if key_id != self._hover_key:
            self._set_hover_key(key_id, now)

        if key_id is None:
            return

        if self._scanning_enabled:
            # In scanning mode dwell hover is optional; scanning focus drives selection.
            return

        self._update_dwell(key_id, x, y, now)

    def get_key_bounds(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        gx = self.geometry().x()
        gy = self.geometry().y()
        for key_id, rect in self._key_rects.items():
            spec = self._key_specs[key_id]
            out[key_id] = {
                "label": spec.label,
                "kind": spec.kind,
                "rect": (int(gx + rect.x()), int(gy + rect.y()), int(rect.width()), int(rect.height())),
                "language": self._language,
                "layout": self._layout_layer,
            }
        return out

    def trigger_key(self, key_id: str) -> bool:
        if key_id not in self._key_specs:
            return False
        self._activate_key(key_id)
        return True

    def set_language(self, language: str) -> None:
        if language not in self._language_cycle:
            return
        self._language = language
        self._predictor.set_language(language)
        self._rebuild_layout()

    def set_theme(self, theme_name: str, custom: Optional[KeyboardTheme] = None) -> None:
        if custom is not None:
            self._theme = custom
        elif theme_name in THEMES:
            self._theme = THEMES[theme_name]
        self.update()

    def set_keyboard_mode(self, mode: str) -> None:
        if mode not in {"floating", "docked", "split"}:
            return
        self._keyboard_mode = mode
        self._rebuild_layout()
        if self.isVisible():
            x, y = QApplication.cursor().pos().x(), QApplication.cursor().pos().y()
            self._place_window(x, y)

    def set_scale(self, scale: float) -> None:
        self.config.scale = min(max(scale, 0.65), 2.1)
        self._rebuild_layout()

    def set_dwell_profile(self, dwell_time: float, move_threshold: float, predwell_delay: float) -> None:
        self.config.dwell_time = max(0.15, dwell_time)
        self.config.move_threshold = max(1.0, move_threshold)
        self.config.predwell_delay = max(0.0, predwell_delay)

    def set_scanning_mode(self, enabled: bool, interval_ms: Optional[int] = None) -> None:
        self._scanning_enabled = enabled
        self._scanning_row_mode = True
        self._scan_row = 0
        self._scan_col = 0
        if interval_ms is not None:
            self.config.scanning_interval_ms = max(180, interval_ms)
        if enabled:
            self._scan_timer.start(self.config.scanning_interval_ms)
        else:
            self._scan_timer.stop()
        self.update()

    def scan_select(self) -> None:
        if not self._scanning_enabled or not self._scan_rows:
            return
        row_keys = self._scan_rows[self._scan_row]
        if not row_keys:
            return
        if self._scanning_row_mode:
            self._scanning_row_mode = False
            self._scan_col = 0
            self._scan_timer.start(self.config.scan_column_interval_ms)
        else:
            key_id = row_keys[min(self._scan_col, len(row_keys) - 1)]
            self._activate_key(key_id)
            self._scanning_row_mode = True
            self._scan_col = 0
            self._scan_timer.start(self.config.scanning_interval_ms)

    def register_plugin(self, plugin: KeyboardPlugin) -> None:
        self._plugins.append(plugin)
        if hasattr(plugin, "on_registered"):
            plugin.on_registered(self)

    def set_feedback_hooks(
        self,
        audio: Optional[Callable[[str], None]] = None,
        haptic: Optional[Callable[[str], None]] = None,
        tts: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._audio_hook = audio
        self._haptic_hook = haptic
        self._tts_hook = tts

    # --------------------------- Internal mechanics -------------------------

    def _build_input_injector(self) -> BaseInputInjector:
        try:
            import pynput  # noqa: F401

            return PynputInputInjector()
        except Exception:
            return NullInputInjector()

    def _place_window(self, x: int, y: int) -> None:
        screen = QApplication.primaryScreen().geometry()
        margin = 10

        if self._snap_anchor != "cursor":
            if self._snap_anchor == "top_left":
                px, py = screen.left() + margin, screen.top() + margin
            elif self._snap_anchor == "top_right":
                px, py = screen.right() - self.width() - margin, screen.top() + margin
            elif self._snap_anchor == "bottom_left":
                px, py = screen.left() + margin, screen.bottom() - self.height() - margin
            elif self._snap_anchor == "bottom_right":
                px, py = screen.right() - self.width() - margin, screen.bottom() - self.height() - margin
            else:
                px = screen.center().x() - self.width() // 2
                py = screen.bottom() - self.height() - margin
        else:
            if self._keyboard_mode == "docked":
                px = screen.center().x() - self.width() // 2
                py = screen.bottom() - self.height() - 24
            else:
                px = x + 20
                py = y + 20

        px = max(screen.left() + margin, min(px, screen.right() - self.width() - margin))
        py = max(screen.top() + margin, min(py, screen.bottom() - self.height() - margin))
        self.move(int(px), int(py))

    def _rebuild_layout(self) -> None:
        self._key_specs.clear()
        self._key_rects.clear()
        self._clip_item_ids = []

        rows = self._layout_rows()

        ks = self.config.key_size * self.config.scale
        gap = self.config.key_gap * self.config.scale
        pad = 8 * self.config.scale
        show_suggestions = self.config.autocomplete_enabled and self.config.suggestion_count > 0
        suggestion_h = ks * 0.8 if show_suggestions else 0.0

        y = pad + suggestion_h + (gap if show_suggestions else 0.0)
        max_w = 0.0
        self._scan_rows = []

        for r_idx, row in enumerate(rows):
            row_specs = [self._spec_from_token(token, r_idx, c_idx) for c_idx, token in enumerate(row)]
            row_units = sum(spec.width_units for spec in row_specs)
            row_w = row_units * ks + max(0, len(row_specs) - 1) * gap
            max_w = max(max_w, row_w)

            x = pad
            split_extra = 0.0
            scan_row_ids: List[str] = []
            for c_idx, spec in enumerate(row_specs):
                if self._keyboard_mode == "split" and self._layout_layer == "letters" and c_idx == len(row_specs) // 2:
                    split_extra = ks * 0.9
                w = spec.width_units * ks
                rect = QRectF(x + split_extra, y, w, ks)
                self._key_specs[spec.key_id] = spec
                self._key_rects[spec.key_id] = rect
                scan_row_ids.append(spec.key_id)
                x += w + gap
            self._scan_rows.append(scan_row_ids)
            y += ks + gap

        panel_w = max_w + pad * 2 + (ks * 0.9 if self._keyboard_mode == "split" and self._layout_layer == "letters" else 0)
        panel_h = y + pad
        clip_items = self._clipboard.items()[:4] if self._clipboard_panel else []
        if clip_items:
            clip_h = max(24.0, 24 * self.config.scale)
            clip_gap = max(3.0, gap * 0.55)
            clip_area_h = len(clip_items) * clip_h + max(0, len(clip_items) - 1) * clip_gap + pad
            panel_h += clip_area_h
        self.setFixedSize(int(panel_w), int(panel_h))

        # Suggestion strip rectangles (optional).
        self._suggestion_rects: List[QRectF] = []
        if show_suggestions:
            s_gap = gap
            total_s_width = panel_w - pad * 2 - s_gap * (self.config.suggestion_count - 1)
            cell_w = total_s_width / max(1, self.config.suggestion_count)
            sx = pad
            sy = pad
            for i in range(self.config.suggestion_count):
                rect = QRectF(sx, sy, cell_w, suggestion_h)
                self._suggestion_rects.append(rect)
                sugg_id = f"sugg:{i}"
                self._key_specs[sugg_id] = KeySpec(sugg_id, "", kind="suggestion")
                self._key_rects[sugg_id] = rect
                sx += cell_w + s_gap

        if clip_items:
            clip_h = max(24.0, 24 * self.config.scale)
            clip_gap = max(3.0, gap * 0.55)
            clip_w = panel_w - pad * 2
            cy = panel_h - (len(clip_items) * clip_h + max(0, len(clip_items) - 1) * clip_gap + pad)
            for idx, item in enumerate(clip_items):
                key_id = f"clip_item:{idx}"
                clipped = item if len(item) <= 64 else (item[:61] + "...")
                rect = QRectF(pad, cy, clip_w, clip_h)
                self._key_specs[key_id] = KeySpec(
                    key_id,
                    clipped,
                    kind="clip_item",
                    payload=item,
                )
                self._key_rects[key_id] = rect
                self._clip_item_ids.append(key_id)
                cy += clip_h + clip_gap

        self._progress = {key_id: 0.0 for key_id in self._key_rects.keys()}
        self._pressed_flash = {key_id: 0.0 for key_id in self._key_rects.keys()}
        self._refresh_suggestions()
        self.update()

    def _layout_rows(self) -> List[List[str]]:
        if self._layout_layer == "settings":
            return [
                ["Dwell-", "Dwell+", "Size-", "Size+", "Gap-", "Gap+"],
                ["Scale-", "Scale+", "Scan", "Mode", "Swipe", "Theme"],
                ["Dark", "Light", "HighC", "Lang", "Snap", "Clip"],
                ["ABC", "123", "Sym", "Emoji", "Done", "Enter"],
            ]

        if self._layout_layer == "emoji":
            pages = [
                ["😀", "😁", "😂", "🤣", "😊", "😍", "😘", "😎", "🤔", "👍"],
                ["👏", "🙏", "🔥", "🎉", "💡", "❤️", "💙", "✅", "⭐", "📌"],
                ["🙂", "🙃", "😉", "😭", "😡", "🤖", "🌍", "🍀", "⚡", "⌛"],
                ["ABC", "123", "Settings", "Space", "Enter"],
            ]
            return pages

        if self._layout_layer == "numbers":
            return [
                ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
                ["@", "#", "$", "_", "&", "-", "+", "(", ")", "/"],
                ["Sym", "*", '"', "'", ";", ":", "!", "?", "Backspace"],
                ["ABC", "Emoji", ".", "Space", "Enter", "Settings", "Snap"],
            ]

        if self._layout_layer == "symbols":
            return [
                ["[", "]", "{", "}", "#", "%", "^", "*", "+", "="],
                ["_", "\\", "|", "~", "<", ">", "€", "£", "¥", "•"],
                ["123", ".", ",", "?", "!", "'", '"', "Emoji", "Backspace"],
                ["ABC", "@", "/", "Space", "Enter", "Settings", "Snap"],
            ]

        if self._layout_layer == "functions":
            return [
                ["F1", "F2", "F3", "F4", "F5", "F6"],
                ["F7", "F8", "F9", "F10", "F11", "F12"],
                ["Undo", "Redo", "Copy", "Paste", "Scan", "Theme", "Snap"],
                ["ABC", "Num", "Sym", "Emoji", "Settings", "Space", "Enter"],
            ]

        # letters
        row1 = list("qwertyuiop")
        if self._language == "es":
            row2 = ["a", "s", "d", "f", "g", "h", "j", "k", "l", "ñ"]
        elif self._language == "fr":
            row2 = ["a", "z", "e", "r", "t", "y", "u", "i", "o", "p"]
            row1 = ["q", "s", "d", "f", "g", "h", "j", "k", "l", "m"]
        else:
            row2 = list("asdfghjkl")

        row3 = ["Shift", "z", "x", "c", "v", "b", "n", "m", "Backspace"]
        row4 = ["123", "Lang", "Emoji", "Space", ".", "Enter", "Settings", "Snap"]
        return [row1, row2, row3, row4]

    def _spec_from_token(self, token: str, _row: int, _col: int) -> KeySpec:
        t = token
        if t == "Space":
            return KeySpec("space", "Space", kind="space", width_units=4.0)
        if t == "Backspace":
            return KeySpec("backspace", "⌫", kind="backspace", width_units=1.8)
        if t == "Shift":
            return KeySpec("shift", "Shift", kind="modifier", width_units=1.7)
        if t == "Ctrl":
            return KeySpec("ctrl", "Ctrl", kind="modifier", width_units=1.5)
        if t == "Alt":
            return KeySpec("alt", "Alt", kind="modifier", width_units=1.5)
        if t == "Enter":
            return KeySpec("enter", "Enter", kind="enter", width_units=1.9)
        if t in {"123", "Num"}:
            return KeySpec("to_numbers", "123", kind="layer", width_units=1.6)
        if t == "Sym":
            return KeySpec("to_symbols", "Sym", kind="layer", width_units=1.6)
        if t in {"ABC", "Letters"}:
            return KeySpec("to_letters", "ABC", kind="layer", width_units=1.6)
        if t == "Fn":
            return KeySpec("to_functions", "Fn", kind="layer", width_units=1.5)
        if t == "Settings":
            return KeySpec("to_settings", "⚙", kind="layer", width_units=1.3)
        if t == "Done":
            return KeySpec("to_letters", "Done", kind="layer", width_units=1.6)
        if t == "Lang":
            return KeySpec("lang", "Lang", kind="language", width_units=1.5)
        if t == "Emoji":
            return KeySpec("to_emoji", "😊", kind="layer", width_units=1.5)
        if t == "Emoji<":
            return KeySpec("emoji_prev", "◀", kind="emoji_nav", width_units=1.5)
        if t == "Emoji>":
            return KeySpec("emoji_next", "▶", kind="emoji_nav", width_units=1.5)
        if t == "Clip":
            return KeySpec("clipboard", "Clip", kind="clipboard", width_units=1.6)
        if t == "Copy":
            return KeySpec("copy", "Copy", kind="system", width_units=1.6)
        if t == "Paste":
            return KeySpec("paste", "Paste", kind="system", width_units=1.6)
        if t == "Undo":
            return KeySpec("undo", "Undo", kind="system", width_units=1.6)
        if t == "Redo":
            return KeySpec("redo", "Redo", kind="system", width_units=1.6)
        if t == "Theme":
            return KeySpec("theme", "Theme", kind="system", width_units=1.6)
        if t == "Mode":
            return KeySpec("mode_cycle", "Mode", kind="system", width_units=1.6)
        if t == "Snap":
            return KeySpec("snap", "Snap", kind="system", width_units=1.5)
        if t == "Swipe":
            return KeySpec("toggle_swipe", "Swipe", kind="system", width_units=1.5)
        if t == "Dark":
            return KeySpec("theme_dark", "Dark", kind="system", width_units=1.4)
        if t == "Light":
            return KeySpec("theme_light", "Light", kind="system", width_units=1.4)
        if t == "HighC":
            return KeySpec("theme_high_contrast", "HighC", kind="system", width_units=1.6)
        if t == "Dwell-":
            return KeySpec("set_dwell_down", "Dwell-", kind="setting", width_units=1.6)
        if t == "Dwell+":
            return KeySpec("set_dwell_up", "Dwell+", kind="setting", width_units=1.6)
        if t == "Size-":
            return KeySpec("set_size_down", "Size-", kind="setting", width_units=1.5)
        if t == "Size+":
            return KeySpec("set_size_up", "Size+", kind="setting", width_units=1.5)
        if t == "Gap-":
            return KeySpec("set_gap_down", "Gap-", kind="setting", width_units=1.5)
        if t == "Gap+":
            return KeySpec("set_gap_up", "Gap+", kind="setting", width_units=1.5)
        if t == "Scale-":
            return KeySpec("set_scale_down", "Scale-", kind="setting", width_units=1.6)
        if t == "Scale+":
            return KeySpec("set_scale_up", "Scale+", kind="setting", width_units=1.6)
        if t == "Scan":
            return KeySpec("scan", "Scan", kind="access", width_units=1.6)
        if t.upper().startswith("F") and t[1:].isdigit():
            return KeySpec(f"{t.lower()}", t.upper(), kind="function", width_units=1.0)
        return KeySpec(f"char:{_row}:{_col}:{t}", t, kind="char", width_units=1.0, payload=t)

    def _refresh_suggestions(self) -> None:
        if not self.config.autocomplete_enabled or self.config.suggestion_count <= 0:
            self._suggestions = []
            self.suggestionsChanged.emit([])
            self.update()
            return
        self._suggestions = self._predictor.suggest(
            prefix=self._current_word,
            last_word=self._last_word,
            limit=self.config.suggestion_count,
        )
        self.suggestionsChanged.emit(list(self._suggestions))
        self.update()

    def _hit_test_global(self, gx: float, gy: float) -> Optional[str]:
        lx = gx - self.geometry().x()
        ly = gy - self.geometry().y()
        point = QPointF(lx, ly)
        for key_id, rect in self._key_rects.items():
            if rect.contains(point):
                return key_id
        return None

    def _set_hover_key(self, key_id: Optional[str], now: float) -> None:
        self._hover_key = key_id
        self._hover_started_at = now if key_id else None
        self._hover_stable_since = now if key_id else None
        self._hover_last_pos = None
        self._hover_hold_emitted = False

        for k in self._progress.keys():
            if k != key_id:
                self._progress[k] = 0.0

        if key_id:
            meta = self._key_meta(key_id)
            self.onKeyHover.emit(meta)
            self._plugin_dispatch("on_key_hover", meta)
            if key_id.startswith("char:"):
                letter = self._key_specs[key_id].label.lower()
                if letter.isalpha():
                    if self._swipe_letters and (now - self._swipe_last_t) > self.config.swipe_link_window:
                        self._swipe_letters = []
                    if not self._swipe_letters or self._swipe_letters[-1] != letter:
                        self._swipe_letters.append(letter)
                    self._swipe_last_t = now
        self.update()

    def _update_dwell(self, key_id: str, x: float, y: float, now: float) -> None:
        if self._hover_last_pos is None:
            self._hover_last_pos = (x, y)
            return

        dx = x - self._hover_last_pos[0]
        dy = y - self._hover_last_pos[1]
        self._hover_last_pos = (x, y)
        dist = math.hypot(dx, dy)

        if dist > self.config.move_threshold:
            self._hover_started_at = now
            self._hover_stable_since = now
            self._progress[key_id] = 0.0
            self._hover_hold_emitted = False
            self.update()
            return

        if self._hover_stable_since is None:
            self._hover_stable_since = now
            return

        if now - self._hover_stable_since < self.config.predwell_delay:
            return

        if self._hover_started_at is None:
            self._hover_started_at = now

        elapsed = now - self._hover_started_at
        prog = min(1.0, elapsed / max(0.08, self.config.dwell_time))
        self._progress[key_id] = prog

        if (not self._hover_hold_emitted) and prog >= 0.55:
            self._hover_hold_emitted = True
            meta = self._key_meta(key_id)
            self.onKeyHold.emit(meta)
            self._plugin_dispatch("on_key_hold", meta)

        if prog >= 1.0 and now - self._last_fire_at >= self.config.cooldown:
            self._last_fire_at = now
            self._progress[key_id] = 0.0
            self._activate_key(key_id)
            self._hover_started_at = now
            self._hover_hold_emitted = False

        self.update()

    def _activate_key(self, key_id: str) -> None:
        spec = self._key_specs.get(key_id)
        if not spec:
            return

        self._pressed_flash[key_id] = 1.0
        meta = self._key_meta(key_id)
        self.onKeyPress.emit(meta)
        self._plugin_dispatch("on_key_press", meta)
        self._feedback("key_press")

        if key_id.startswith("sugg:"):
            idx = int(key_id.split(":", 1)[1])
            if idx < len(self._suggestions):
                self._commit_suggestion(self._suggestions[idx])
            return

        kind = spec.kind

        if kind == "char":
            self._type_character(spec.payload or spec.label)
            return

        if kind == "space":
            self._commit_space()
            return

        if kind == "backspace":
            self._handle_backspace()
            return

        if kind == "enter":
            self._commit_enter()
            return

        if kind == "modifier":
            self._toggle_modifier(key_id)
            return

        if kind == "layer":
            self._switch_layer(key_id)
            return

        if kind == "language":
            self._rotate_language()
            return

        if kind == "setting":
            self._handle_setting_action(key_id)
            return

        if kind == "emoji_nav":
            if key_id == "emoji_next":
                self._emoji_page = (self._emoji_page + 1) % 1
            elif key_id == "emoji_prev":
                self._emoji_page = (self._emoji_page - 1) % 1
            self._rebuild_layout()
            return

        if kind == "clip_item":
            payload = spec.payload or spec.label
            self._input.type_text(payload)
            return

        if kind == "clipboard":
            self._clipboard_panel = not self._clipboard_panel
            self._rebuild_layout()
            return

        if kind == "function":
            self._input.tap(spec.label.lower())
            return

        if kind == "system":
            self._handle_system_action(key_id)
            return

        if kind == "access":
            self.set_scanning_mode(not self._scanning_enabled)
            return

    def _commit_suggestion(self, suggestion: str) -> None:
        if not suggestion:
            return

        if self._current_word and suggestion.lower().startswith(self._current_word.lower()):
            suffix = suggestion[len(self._current_word):]
            if suffix:
                self._input.type_text(suffix)
        else:
            self._input.type_text(suggestion)
        self._input.tap("space")

        if self._current_word:
            self._predictor.learn_word(suggestion, self._last_word)
        self._last_word = suggestion
        self._current_word = ""
        self._refresh_suggestions()

    def _type_character(self, char: str) -> None:
        out = char
        if len(char) == 1 and char.isalpha():
            should_upper = self._caps_lock or self._shift_once
            out = char.upper() if should_upper else char.lower()

        if self._ctrl:
            self._input.chord([self._platform_ctrl_key()], out.lower())
        elif self._alt:
            self._input.chord(["alt"], out.lower())
        else:
            if len(char) == 1 and char.isalpha() and (self._caps_lock or self._shift_once):
                self._input.chord(["shift"], char.lower())
            else:
                self._input.type_text(out)

        if len(out) == 1 and out.isalpha():
            self._current_word += out
        elif out in "'-":
            self._current_word += out
        else:
            self._current_word = ""

        if self._shift_once:
            self._shift_once = False

        self._refresh_suggestions()

    def _commit_space(self) -> None:
        self._input.tap("space")

        committed = self._current_word.strip()
        if committed:
            self._predictor.learn_word(committed, self._last_word)
            self._last_word = committed
        self._current_word = ""

        if self._shift_once:
            self._shift_once = False

        self._refresh_suggestions()

    def _commit_enter(self) -> None:
        self._input.tap("enter")

        committed = self._current_word.strip()
        if committed:
            self._predictor.learn_word(committed, self._last_word)
            self._last_word = committed
        self._current_word = ""

        if self._shift_once:
            self._shift_once = False

        self._refresh_suggestions()

    def _handle_backspace(self) -> None:
        self._input.tap("backspace")
        if self._current_word:
            self._current_word = self._current_word[:-1]
        self._refresh_suggestions()

    def _toggle_modifier(self, key_id: str) -> None:
        if key_id == "shift":
            # tap Shift cycles: off -> once -> caps lock -> off
            if not self._shift_once and not self._caps_lock:
                self._shift_once = True
            elif self._shift_once and not self._caps_lock:
                self._shift_once = False
                self._caps_lock = True
            else:
                self._shift_once = False
                self._caps_lock = False
        elif key_id == "ctrl":
            self._ctrl = not self._ctrl
            self._input.press_modifier(self._platform_ctrl_key(), self._ctrl)
        elif key_id == "alt":
            self._alt = not self._alt
            self._input.press_modifier("alt", self._alt)
        self.update()

    def _switch_layer(self, key_id: str) -> None:
        mapping = {
            "to_numbers": "numbers",
            "to_symbols": "symbols",
            "to_letters": "letters",
            "to_functions": "functions",
            "to_emoji": "emoji",
            "to_settings": "settings",
        }
        new_layer = mapping.get(key_id, "letters")
        self._layout_layer = new_layer
        self._rebuild_layout()

    def _rotate_language(self) -> None:
        idx = self._language_cycle.index(self._language)
        self._language = self._language_cycle[(idx + 1) % len(self._language_cycle)]
        self._predictor.set_language(self._language)
        self._rebuild_layout()
        self._feedback("language")

    def _handle_system_action(self, key_id: str) -> None:
        ctrl = self._platform_ctrl_key()
        if key_id == "copy":
            self._input.chord([ctrl], "c")
            self._feedback("copy")
            return
        if key_id == "paste":
            self._input.chord([ctrl], "v")
            self._feedback("paste")
            return
        if key_id == "undo":
            self._input.chord([ctrl], "z")
            self._feedback("undo")
            return
        if key_id == "redo":
            if sys.platform == "darwin":
                self._input.chord([ctrl, "shift"], "z")
            else:
                self._input.chord([ctrl], "y")
            self._feedback("redo")
            return
        if key_id == "theme":
            order = ["dark", "light", "high_contrast"]
            current_idx = order.index(self._theme.name)
            self._theme = THEMES[order[(current_idx + 1) % len(order)]]
            self.update()
            return
        if key_id == "mode_cycle":
            self._cycle_keyboard_mode()
            return
        if key_id == "snap":
            self._cycle_snap_anchor()
            return
        if key_id == "toggle_swipe":
            self._swipe_enabled = not self._swipe_enabled
            self.update()
            return
        if key_id == "theme_dark":
            self._theme = THEMES["dark"]
            self.update()
            return
        if key_id == "theme_light":
            self._theme = THEMES["light"]
            self.update()
            return
        if key_id == "theme_high_contrast":
            self._theme = THEMES["high_contrast"]
            self.update()
            return

    def _apply_autocorrect_before_separator(self) -> None:
        return

    def _handle_setting_action(self, key_id: str) -> None:
        if key_id == "set_dwell_down":
            self.config.dwell_time = max(0.25, self.config.dwell_time - 0.05)
        elif key_id == "set_dwell_up":
            self.config.dwell_time = min(2.0, self.config.dwell_time + 0.05)
        elif key_id == "set_size_down":
            self.config.key_size = max(36, self.config.key_size - 2)
            self._rebuild_layout()
        elif key_id == "set_size_up":
            self.config.key_size = min(74, self.config.key_size + 2)
            self._rebuild_layout()
        elif key_id == "set_gap_down":
            self.config.key_gap = max(2, self.config.key_gap - 1)
            self._rebuild_layout()
        elif key_id == "set_gap_up":
            self.config.key_gap = min(16, self.config.key_gap + 1)
            self._rebuild_layout()
        elif key_id == "set_scale_down":
            self.set_scale(self.config.scale - 0.05)
        elif key_id == "set_scale_up":
            self.set_scale(self.config.scale + 0.05)
        self.update()

    def _cycle_keyboard_mode(self) -> None:
        order = ["floating", "docked", "split"]
        idx = order.index(self._keyboard_mode)
        self._keyboard_mode = order[(idx + 1) % len(order)]
        if self._keyboard_mode != "floating":
            self._snap_anchor = "cursor"
            self._snap_index = 0
        x, y = QApplication.cursor().pos().x(), QApplication.cursor().pos().y()
        self._rebuild_layout()
        self._place_window(x, y)

    def _cycle_snap_anchor(self) -> None:
        self._snap_index = (self._snap_index + 1) % len(self._snap_cycle)
        self._snap_anchor = self._snap_cycle[self._snap_index]
        x, y = QApplication.cursor().pos().x(), QApplication.cursor().pos().y()
        self._place_window(x, y)
        self.update()

    def _commit_swipe_path(self) -> None:
        letters = list(self._swipe_letters)
        self._swipe_letters = []
        if len(letters) < 3:
            return
        word = self._predictor.decode_swipe(letters)
        if not word:
            return
        self._input.type_text(word)
        self._input.tap("space")
        self._predictor.learn_word(word, self._last_word)
        self._last_word = word
        self._current_word = ""
        self._feedback("swipe")
        self._refresh_suggestions()

    def _on_clipboard_history_changed(self, _items: list) -> None:
        if self._clipboard_panel:
            self._rebuild_layout()

    def _scan_advance(self) -> None:
        if not self._scanning_enabled or not self._scan_rows:
            return

        if self._scanning_row_mode:
            self._scan_row = (self._scan_row + 1) % len(self._scan_rows)
        else:
            row_keys = self._scan_rows[self._scan_row]
            if row_keys:
                self._scan_col = (self._scan_col + 1) % len(row_keys)
        self.update()

    def _on_anim(self) -> None:
        changed = False
        for key_id, value in list(self._pressed_flash.items()):
            if value > 0:
                self._pressed_flash[key_id] = max(0.0, value - 0.10)
                changed = True
        if changed:
            self.update()

    def _platform_ctrl_key(self) -> str:
        return "cmd" if sys.platform == "darwin" else "ctrl"

    def _key_meta(self, key_id: str) -> dict:
        spec = self._key_specs[key_id]
        rect = self._key_rects[key_id]
        gx = self.geometry().x() + int(rect.x())
        gy = self.geometry().y() + int(rect.y())
        return {
            "id": key_id,
            "label": spec.label,
            "kind": spec.kind,
            "rect": (gx, gy, int(rect.width()), int(rect.height())),
            "language": self._language,
            "layer": self._layout_layer,
            "modifiers": {
                "shift": self._shift_once,
                "caps": self._caps_lock,
                "ctrl": self._ctrl,
                "alt": self._alt,
            },
        }

    def _plugin_dispatch(self, method_name: str, key_meta: dict) -> None:
        for plugin in self._plugins:
            fn = getattr(plugin, method_name, None)
            if callable(fn):
                try:
                    fn(key_meta)
                except Exception as exc:
                    print(f"[ModernKeyboard] Plugin error in {method_name}: {exc}")

    def _feedback(self, event_name: str) -> None:
        for hook in (self._audio_hook, self._haptic_hook, self._tts_hook):
            if hook:
                try:
                    hook(event_name)
                except Exception as exc:
                    print(f"[ModernKeyboard] Feedback hook error: {exc}")

    # ------------------------------- Painting -------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        # Keyboard panel background.
        bg_rect = QRectF(0, 0, self.width(), self.height())
        p.setPen(Qt.NoPen)
        p.setBrush(self._theme.shadow)
        p.drawRoundedRect(bg_rect.adjusted(2, 4, 0, 0), 14, 14)

        p.setBrush(self._theme.bg)
        p.drawRoundedRect(bg_rect.adjusted(0, 0, -2, -2), 14, 14)

        self._paint_suggestions(p)
        self._paint_keys(p)
        self._paint_status(p)

        p.end()

    def _paint_suggestions(self, p: QPainter) -> None:
        if not self._suggestion_rects:
            return
        title_font = QFont("Segoe UI", max(8, int(10 * self.config.scale)), QFont.Medium)
        p.setFont(title_font)

        for i, rect in enumerate(self._suggestion_rects):
            key_id = f"sugg:{i}"
            hover = self._hover_key == key_id
            flash = self._pressed_flash.get(key_id, 0.0)

            color = QColor(self._theme.special)
            if hover:
                color = QColor(self._theme.key_hover)
            if flash > 0:
                color = QColor(self._theme.key_pressed)
                color.setAlpha(200)

            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(rect, 10, 10)

            p.setPen(self._theme.text)
            text = self._suggestions[i] if i < len(self._suggestions) else ""
            p.drawText(rect, Qt.AlignCenter, text)

    def _paint_keys(self, p: QPainter) -> None:
        font = QFont("Segoe UI", max(9, int(12 * self.config.scale)), QFont.Medium)
        small = QFont("Segoe UI", max(8, int(10 * self.config.scale)), QFont.Medium)

        for key_id, rect in self._key_rects.items():
            if key_id.startswith("sugg:"):
                continue

            spec = self._key_specs[key_id]
            hover = self._hover_key == key_id
            progress = self._progress.get(key_id, 0.0)
            flash = self._pressed_flash.get(key_id, 0.0)

            if spec.kind in {"modifier", "layer", "clipboard", "system", "access", "language", "clip_item", "setting"}:
                fill = QColor(self._theme.special)
            else:
                fill = QColor(self._theme.key)

            if hover:
                fill = QColor(self._theme.key_hover)
            if progress > 0:
                blend = QColor(self._theme.key_hold)
                blend.setAlpha(int(90 + progress * 150))
                fill = blend
            if flash > 0:
                pressed = QColor(self._theme.key_pressed)
                pressed.setAlpha(int(120 + flash * 120))
                fill = pressed

            # Visual state for sticky modifiers.
            if key_id == "shift" and (self._shift_once or self._caps_lock):
                fill = QColor(100, 180, 255, 230)
            if key_id == "ctrl" and self._ctrl:
                fill = QColor(100, 180, 255, 230)
            if key_id == "alt" and self._alt:
                fill = QColor(100, 180, 255, 230)

            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(rect, 10, 10)

            # Draw dwell arc for current key.
            if progress > 0:
                margin = 3.0
                arc_rect = QRectF(
                    rect.x() + margin,
                    rect.y() + margin,
                    rect.width() - margin * 2,
                    rect.height() - margin * 2,
                )
                p.setPen(QPen(C_KB_PROG, 2.2, Qt.SolidLine, Qt.RoundCap))
                p.setBrush(Qt.NoBrush)
                p.drawArc(arc_rect, 90 * 16, -int(progress * 360 * 16))

            p.setPen(self._theme.text)
            if spec.kind == "clip_item":
                p.setFont(small)
                p.drawText(rect.adjusted(8, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft, spec.label)
            else:
                p.setFont(small if len(spec.label) > 2 else font)
                p.drawText(rect, Qt.AlignCenter, spec.label)

        if self._scanning_enabled:
            self._paint_scan_focus(p)

    def _paint_clipboard_history(self, p: QPainter) -> None:
        items = self._clipboard.items()[:4]
        if not items:
            return

        w = self.width() - 16
        h = 24
        y = self.height() - (len(items) * (h + 4)) - 8
        x = 8

        font = QFont("Segoe UI", max(7, int(9 * self.config.scale)), QFont.Medium)
        p.setFont(font)

        for item in items:
            rect = QRectF(x, y, w, h)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(self._theme.special))
            p.drawRoundedRect(rect, 7, 7)

            p.setPen(self._theme.hint)
            clipped = item if len(item) <= 64 else (item[:61] + "...")
            p.drawText(rect.adjusted(8, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft, clipped)
            y += h + 4

    def _paint_scan_focus(self, p: QPainter) -> None:
        if not self._scan_rows:
            return

        p.setPen(QPen(QColor(255, 208, 70, 255), 2.2))
        p.setBrush(Qt.NoBrush)

        if self._scanning_row_mode:
            row = self._scan_rows[self._scan_row]
            if not row:
                return
            rects = [self._key_rects[key] for key in row if key in self._key_rects]
            if not rects:
                return
            left = min(r.left() for r in rects)
            top = min(r.top() for r in rects)
            right = max(r.right() for r in rects)
            bottom = max(r.bottom() for r in rects)
            p.drawRoundedRect(QRectF(left - 3, top - 3, (right - left) + 6, (bottom - top) + 6), 12, 12)
        else:
            row = self._scan_rows[self._scan_row]
            if not row:
                return
            key_id = row[min(self._scan_col, len(row) - 1)]
            rect = self._key_rects.get(key_id)
            if rect:
                p.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 12, 12)

    def _paint_status(self, p: QPainter) -> None:
        info = [
            f"Lang: {self._language.upper()}",
            f"Mode: {self._keyboard_mode}",
            f"Layer: {self._layout_layer}",
            f"Snap: {self._snap_anchor}",
        ]
        if self._caps_lock:
            info.append("Caps")
        elif self._shift_once:
            info.append("Shift")
        if self._scanning_enabled:
            info.append("Scan")

        text = " | ".join(info)
        p.setPen(self._theme.hint)
        p.setFont(QFont("Segoe UI", max(7, int(9 * self.config.scale)), QFont.Medium))
        p.drawText(QRectF(10, self.height() - 22, self.width() - 20, 16), Qt.AlignRight | Qt.AlignVCenter, text)


class KeyboardManager(QObject):
    """Compatibility wrapper for overlay integration."""

    def __init__(self):
        super().__init__()
        self._kb = ModernOnScreenKeyboard()

    def show_near(self, x: int, y: int):
        self._kb.show_near(x, y)

    def hide(self):
        self._kb.hide()

    def contains(self, x: int, y: int) -> bool:
        return self._kb.contains(x, y)

    def update_cursor(self, x: int, y: int):
        self._kb.update_cursor(x, y)

    def isVisible(self) -> bool:
        return self._kb.isVisible()

    def uses_external_dwell(self) -> bool:
        return False

    def keyboard(self) -> ModernOnScreenKeyboard:
        return self._kb
