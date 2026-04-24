"""
Modern On-Screen Keyboard — Gboard-inspired, v5
================================================
Cross-platform keyboard widget for dwell/head/eye-tracking input.

Compatibility interface (overlay integration points):
  show_near(x, y)  hide()  contains(x, y)  update_cursor(x, y)
  isVisible()      uses_external_dwell()

Design goals
  • Gboard look: rounded pill keys, pastel accent, suggestion strip
  • Fully draggable floating window — grab anywhere on the bg pill
  • System language detection via QLocale + Babel-free fallback
  • Arabic (RTL) layout support
  • Modifier toggle-on-second-hover: hover Shift/Ctrl again → off
  • Polished settings panel with sliders and live preview
  • Dwell input, scanning mode, swipe, prediction, autocorrect
  • Offline-first privacy model; optional persistence
"""

from __future__ import annotations

import difflib
import json
import locale
import math
import os
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from PySide6.QtCore import (
    QLocale, QObject, QPoint, QPointF, QRectF, QSize, Qt,
    QTimer, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QLinearGradient, QPainter,
    QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QWidget


# ═══════════════════════════════════════════════════════════════════════════
#  PALETTE  (Gboard Material-You dark defaults)
# ═══════════════════════════════════════════════════════════════════════════
C_KB_PROG = QColor(130, 170, 255, 255)   # used by overlay ring


def _rgba(r, g, b, a=255): return QColor(r, g, b, a)


@dataclass
class GboardTheme:
    name: str
    # window
    win_bg:      QColor   # keyboard panel
    # key surfaces
    key_normal:  QColor
    key_action:  QColor   # space, enter, shift (inactive)
    key_active:  QColor   # pressed / dwell complete flash
    key_mod_on:  QColor   # modifier held/toggled ON
    key_hover:   QColor
    key_dwell:   QColor   # progress fill
    # suggestion strip
    sugg_bg:     QColor
    sugg_divider:QColor
    # text
    text_key:    QColor
    text_hint:   QColor
    text_sugg:   QColor
    # ring / arc
    dwell_arc:   QColor
    # shadow
    shadow:      QColor


THEMES: Dict[str, GboardTheme] = {
    "dark": GboardTheme(
        name="dark",
        win_bg      = _rgba(40, 44, 54, 248),
        key_normal  = _rgba(60, 65, 80, 255),
        key_action  = _rgba(50, 55, 70, 255),
        key_active  = _rgba(100, 180, 130, 240),
        key_mod_on  = _rgba( 90, 145, 255, 240),
        key_hover   = _rgba( 80, 100, 170, 240),
        key_dwell   = _rgba( 90, 145, 255, 170),
        sugg_bg     = _rgba( 30,  34,  44, 255),
        sugg_divider= _rgba( 70,  75,  90, 200),
        text_key    = _rgba(230, 232, 240, 255),
        text_hint   = _rgba(160, 165, 180, 255),
        text_sugg   = _rgba(200, 205, 220, 255),
        dwell_arc   = _rgba(130, 170, 255, 255),
        shadow      = _rgba(  0,   0,   0, 100),
    ),
    "light": GboardTheme(
        name="light",
        win_bg      = _rgba(243, 244, 249, 248),
        key_normal  = _rgba(255, 255, 255, 255),
        key_action  = _rgba(213, 220, 235, 255),
        key_active  = _rgba(130, 210, 160, 240),
        key_mod_on  = _rgba( 90, 145, 255, 240),
        key_hover   = _rgba(200, 215, 255, 240),
        key_dwell   = _rgba( 90, 145, 255, 130),
        sugg_bg     = _rgba(235, 238, 245, 255),
        sugg_divider= _rgba(200, 205, 218, 200),
        text_key    = _rgba( 30,  35,  50, 255),
        text_hint   = _rgba(100, 110, 130, 255),
        text_sugg   = _rgba( 50,  60,  80, 255),
        dwell_arc   = _rgba( 70, 120, 255, 255),
        shadow      = _rgba(100, 110, 130, 80),
    ),
    "amoled": GboardTheme(
        name="amoled",
        win_bg      = _rgba(  0,   0,   0, 255),
        key_normal  = _rgba( 22,  22,  22, 255),
        key_action  = _rgba( 14,  14,  14, 255),
        key_active  = _rgba(  0, 160,  80, 240),
        key_mod_on  = _rgba( 50, 120, 255, 240),
        key_hover   = _rgba( 40,  60, 140, 240),
        key_dwell   = _rgba( 50, 120, 255, 160),
        sugg_bg     = _rgba(  8,   8,   8, 255),
        sugg_divider= _rgba( 40,  40,  40, 200),
        text_key    = _rgba(245, 245, 245, 255),
        text_hint   = _rgba(140, 140, 140, 255),
        text_sugg   = _rgba(210, 210, 210, 255),
        dwell_arc   = _rgba( 80, 140, 255, 255),
        shadow      = _rgba(  0,   0,   0, 120),
    ),
    "high_contrast": GboardTheme(
        name="high_contrast",
        win_bg      = _rgba(  0,   0,   0, 255),
        key_normal  = _rgba( 30,  30,  30, 255),
        key_action  = _rgba( 10,  10,  10, 255),
        key_active  = _rgba(  0, 200,  80, 255),
        key_mod_on  = _rgba(255, 200,   0, 255),
        key_hover   = _rgba(  0,  80, 200, 255),
        key_dwell   = _rgba(  0,  80, 200, 200),
        sugg_bg     = _rgba(  0,   0,   0, 255),
        sugg_divider= _rgba( 80,  80,  80, 255),
        text_key    = _rgba(255, 255, 255, 255),
        text_hint   = _rgba(200, 200, 200, 255),
        text_sugg   = _rgba(255, 255, 255, 255),
        dwell_arc   = _rgba(255, 200,   0, 255),
        shadow      = _rgba(  0,   0,   0, 180),
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class KeyboardConfig:
    dwell_time:         float = 0.70
    move_threshold:     float = 6.0
    predwell_delay:     float = 0.16
    cooldown:           float = 0.28
    key_w:              int   = 44
    key_h:              int   = 48
    key_gap:            int   = 6
    corner_radius:      int   = 10
    scale:              float = 1.0
    suggestion_count:   int   = 3
    autocomplete_enabled: bool = True
    autocorrect_level:  float = 0.55
    scanning_interval_ms: int = 1200
    scan_column_interval_ms: int = 800
    swipe_link_window:  float = 0.20
    swipe_commit_delay: float = 0.32


# ═══════════════════════════════════════════════════════════════════════════
#  LANGUAGE / LOCALE DETECTION
# ═══════════════════════════════════════════════════════════════════════════
def _detect_system_languages() -> List[str]:
    """
    Return a list of language codes present on the system,
    starting with the default locale, followed by common ones.
    Always includes 'en'. De-duplicated.
    """
    found: List[str] = []

    # QLocale system default
    sys_locale = QLocale.system()
    lang_code = sys_locale.name().split("_")[0].lower()  # e.g. "ar", "fr"
    if lang_code:
        found.append(lang_code)

    # Also read LANG / LC_ALL env
    for var in ("LANG", "LC_ALL", "LC_MESSAGES"):
        val = os.environ.get(var, "")
        if val:
            code = val.split(".")[0].split("_")[0].lower()
            if code and code not in found:
                found.append(code)

    # Ensure 'en' is always present
    if "en" not in found:
        found.append("en")

    # Cap at 5 and de-dup
    seen: set = set()
    result: List[str] = []
    for lang in found:
        if lang not in seen and len(result) < 5:
            seen.add(lang)
            result.append(lang)
    return result


_SYSTEM_LANGUAGES: List[str] = _detect_system_languages()

_RTL_LANGUAGES = {"ar", "he", "fa", "ur"}


# ═══════════════════════════════════════════════════════════════════════════
#  KEY SPEC
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class KeySpec:
    key_id:      str
    label:       str
    kind:        str   = "char"       # char space backspace enter modifier layer language system
    width_units: float = 1.0
    payload:     Optional[str] = None
    icon:        Optional[str] = None  # emoji or symbol override for display


# ═══════════════════════════════════════════════════════════════════════════
#  PLUGIN PROTOCOL
# ═══════════════════════════════════════════════════════════════════════════
class KeyboardPlugin(Protocol):
    def on_registered(self, keyboard: "ModernOnScreenKeyboard") -> None: ...
    def on_key_hover(self, meta: dict) -> None: ...
    def on_key_press(self, meta: dict) -> None: ...
    def on_key_hold(self, meta: dict) -> None: ...


# ═══════════════════════════════════════════════════════════════════════════
#  INPUT INJECTORS
# ═══════════════════════════════════════════════════════════════════════════
class BaseInputInjector:
    def type_text(self, text: str) -> None: raise NotImplementedError
    def tap(self, key_name: str) -> None: raise NotImplementedError
    def chord(self, modifiers: Sequence[str], key_name: str) -> None: raise NotImplementedError
    def press_modifier(self, modifier: str, down: bool) -> None: raise NotImplementedError
    def backspaces(self, count: int) -> None:
        for _ in range(max(0, count)): self.tap("backspace")


class NullInputInjector(BaseInputInjector):
    def type_text(self, text: str) -> None: print(f"[KB] TYPE {text!r}")
    def tap(self, key_name: str) -> None: print(f"[KB] TAP  {key_name}")
    def chord(self, modifiers: Sequence[str], key_name: str) -> None:
        print(f"[KB] CHORD {list(modifiers)} + {key_name}")
    def press_modifier(self, modifier: str, down: bool) -> None:
        print(f"[KB] MOD {modifier} {'↓' if down else '↑'}")


class PynputInputInjector(BaseInputInjector):
    def __init__(self):
        from pynput.keyboard import Controller, Key
        self._ctrl = Controller()
        self._Key  = Key
        self._held: Dict[str, object] = {}

    def _map(self, name: str):
        k = self._Key
        return {
            "enter": k.enter, "backspace": k.backspace, "space": k.space,
            "tab": k.tab, "esc": k.esc, "delete": k.delete,
            "left": k.left, "right": k.right, "up": k.up, "down": k.down,
            "home": k.home, "end": k.end, "pageup": k.page_up, "pagedown": k.page_down,
            "shift": k.shift, "ctrl": k.ctrl, "alt": k.alt, "cmd": k.cmd,
            **{f"f{i}": getattr(k, f"f{i}") for i in range(1, 13)},
        }.get(name.lower(), name)

    def type_text(self, text: str) -> None: self._ctrl.type(text)
    def tap(self, name: str) -> None:
        m = self._map(name); self._ctrl.press(m); self._ctrl.release(m)
    def chord(self, modifiers: Sequence[str], key_name: str) -> None:
        pressed = []
        try:
            for mod in modifiers:
                mk = self._map(mod); self._ctrl.press(mk); pressed.append(mk)
            kk = self._map(key_name); self._ctrl.press(kk); self._ctrl.release(kk)
        finally:
            for mk in reversed(pressed): self._ctrl.release(mk)
    def press_modifier(self, modifier: str, down: bool) -> None:
        mapped = self._map(modifier)
        if down and modifier not in self._held:
            self._ctrl.press(mapped); self._held[modifier] = mapped
        elif not down and modifier in self._held:
            self._ctrl.release(self._held[modifier]); del self._held[modifier]


# ═══════════════════════════════════════════════════════════════════════════
#  OFFLINE PREDICTION ENGINE
# ═══════════════════════════════════════════════════════════════════════════
class OfflinePredictionEngine:
    def __init__(self, language: str = "en"):
        self.language = language
        self._base    = self._base_lexicon(language)
        self._user_freq: Counter[str] = Counter()
        self._next: Dict[Tuple[str, str], int] = {}
        self._load()

    def set_language(self, lang: str) -> None:
        if lang == self.language: return
        self.language = lang
        self._base = self._base_lexicon(lang)

    def suggest(self, prefix: str, last_word: str, limit: int = 3) -> List[str]:
        try: return self._suggest(prefix.lower().strip(), last_word.lower().strip(), limit)
        except Exception: return []

    def _suggest(self, prefix: str, last: str, limit: int) -> List[str]:
        pool = Counter(self._base); pool.update(self._user_freq)
        results: List[Tuple[str, float]] = []
        for word, score in pool.items():
            if prefix and not word.startswith(prefix): continue
            rank = float(score)
            if last: rank += self._next.get((last, word), 0) * 0.9
            if prefix and word == prefix: rank *= 0.8
            results.append((word, rank))
        results.sort(key=lambda x: x[1], reverse=True)
        return [w for w, _ in results[:limit]]

    def autocorrect(self, word: str, aggressiveness: float) -> str:
        w = word.strip()
        if not w: return w
        lower = w.lower()
        pool = set(self._base.keys()) | set(self._user_freq.keys())
        if lower in pool: return w
        cutoff = 0.93 - min(max(aggressiveness, 0.0), 1.0) * 0.18
        candidates = difflib.get_close_matches(lower, list(pool), n=1, cutoff=cutoff)
        if not candidates: return w
        c = candidates[0]
        return c.title() if w.istitle() else (c.upper() if w.isupper() else c)

    def learn_word(self, word: str, prev: str = "") -> None:
        cleaned = "".join(ch for ch in word.lower() if ch.isalpha() or ch in "-'")
        if len(cleaned) < 2: return
        self._user_freq[cleaned] += 1
        if prev:
            key = (prev.lower().strip(), cleaned)
            self._next[key] = self._next.get(key, 0) + 1
        self._save()

    def decode_swipe(self, letters: Sequence[str]) -> Optional[str]:
        norm = [ch.lower() for ch in letters if ch and ch.isalpha()]
        if len(norm) < 3: return None
        start, end, path = norm[0], norm[-1], "".join(norm)
        pool = Counter(self._base); pool.update(self._user_freq)
        best, best_score = None, 0.0
        for word, freq in pool.items():
            if len(word) < 3 or word[0] != start or word[-1] != end: continue
            score = float(freq)
            if self._is_subseq(path, word): score += 2.0
            score -= abs(len(word) - len(path)) * 0.12
            if score > best_score: best_score = score; best = word
        return best

    @staticmethod
    def _is_subseq(path: str, word: str) -> bool:
        it = iter(word); return all(ch in it for ch in path)

    def _user_file(self) -> Path:
        root = Path.home() / ".dwell_keyboard"; root.mkdir(parents=True, exist_ok=True)
        return root / "user_vocab.json"

    def _load(self) -> None:
        path = self._user_file()
        if not path.exists(): return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._user_freq.update(payload.get("freq", {}))
            for pair, cnt in payload.get("next", {}).items():
                parts = pair.split("\t", 1)
                if len(parts) == 2: self._next[(parts[0], parts[1])] = int(cnt)
        except Exception as e: print(f"[KB] vocab load: {e}")

    def _save(self) -> None:
        try:
            payload = {
                "freq": dict(self._user_freq),
                "next": {f"{a}\t{b}": c for (a, b), c in self._next.items()},
            }
            self._user_file().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as e: print(f"[KB] vocab save: {e}")

    @staticmethod
    def _base_lexicon(language: str) -> Counter[str]:
        en = """the be to of and a in that have i it for not on with he as you do at this but
                his by from they we say her she or an will my one all would there their what
                so up out if about who get which go me when make can like time no just him know
                take people into year your good some could them see other than then now look only
                come its over think also back after use two how our work first well way even new
                want because any these give day most us hello yes keyboard dwell accessibility""".split()
        es = """de la que el en y a los del se las por un para con no una su al lo como mas pero
                sus le ya o este si porque esta entre cuando muy sin sobre tambien me hasta hay
                hola teclado escribir accesibilidad sugerencia""".split()
        fr = """de la le et les des en un une pour que dans qui sur pas plus ne au ce il elle
                nous vous ils elles avec comme mais ou donc si est sont clavier bonjour merci""".split()
        ar = """في من إلى على أن هذا ذلك التي الذي كان وكان قد لا هل كيف ما
                مع عن أو أنا نعم لا مرحبا شكرا""".split()
        he = """של עם את זה הוא לא כן אני אבל כי גם כך היה על מה""".split()
        de = """der die das und in zu ist ein eine nicht von es das auf mit sich
                hallo danke ja nein tastatur""".split()
        pt = """de a o para com no na por um uma é que se não também como
                olá obrigado sim não teclado""".split()
        ru = """в и не на он она что с как по это из у было есть
                привет да нет клавиатура спасибо""".split()
        zh = "的 了 在 是 我 有 和 就 不 都 也 都 这 着 一 个".split()
        ja = "の に は が で を と も し れ さ あ い う え お".split()
        mapping = {
            "en": en, "es": es, "fr": fr, "ar": ar, "he": he,
            "de": de, "pt": pt, "ru": ru, "zh": zh, "ja": ja,
        }
        return Counter(mapping.get(language, en))


# ═══════════════════════════════════════════════════════════════════════════
#  CLIPBOARD HISTORY
# ═══════════════════════════════════════════════════════════════════════════
class ClipboardHistory(QObject):
    changed = Signal(list)
    def __init__(self, limit: int = 20):
        super().__init__()
        self._limit = limit
        self._items: deque[str] = deque(maxlen=limit)
        QApplication.clipboard().dataChanged.connect(self._on_change)
    def _on_change(self) -> None:
        text = QApplication.clipboard().text().strip()
        if not text: return
        if self._items and self._items[0] == text: return
        self._items.appendleft(text)
        self.changed.emit(list(self._items))
    def items(self) -> List[str]: return list(self._items)


# ═══════════════════════════════════════════════════════════════════════════
#  LAYOUT BUILDER
# ═══════════════════════════════════════════════════════════════════════════
_LAYOUT_LETTERS: Dict[str, List[List[str]]] = {
    "en": [
        list("qwertyuiop"),
        list("asdfghjkl"),
        ["Shift", "z","x","c","v","b","n","m", "Backspace"],
        ["Sym", "Lang", "Emoji", "Space", ".", ",", "Enter"],
    ],
    "es": [
        list("qwertyuiop"),
        ["a","s","d","f","g","h","j","k","l","ñ"],
        ["Shift", "z","x","c","v","b","n","m", "Backspace"],
        ["Sym", "Lang", "Emoji", "Space", ".", ",", "Enter"],
    ],
    "fr": [
        ["a","z","e","r","t","y","u","i","o","p"],
        ["q","s","d","f","g","h","j","k","l","m"],
        ["Shift", "w","x","c","v","b","n","'",  "Backspace"],
        ["Sym", "Lang", "Emoji", "Space", ".", ",", "Enter"],
    ],
    "de": [
        ["q","w","e","r","t","z","u","i","o","p","ü"],
        ["a","s","d","f","g","h","j","k","l","ö","ä"],
        ["Shift", "y","x","c","v","b","n","m", "Backspace"],
        ["Sym", "Lang", "Emoji", "Space", ".", ",", "Enter"],
    ],
    "pt": [
        list("qwertyuiop"),
        list("asdfghjkl"),
        ["Shift", "z","x","c","v","b","n","m", "Backspace"],
        ["Sym", "Lang", "Emoji", "Space", ".", ",", "Enter"],
    ],
    "ru": [
        ["й","ц","у","к","е","н","г","ш","щ","з","х"],
        ["ф","ы","в","а","п","р","о","л","д","ж","э"],
        ["Shift", "я","ч","с","м","и","т","ь","ъ", "Backspace"],
        ["Sym", "Lang", "Emoji", "Space", ".", ",", "Enter"],
    ],
    "ar": [
        ["ض","ص","ث","ق","ف","غ","ع","ه","خ","ح","ج"],
        ["ش","س","ي","ب","ل","ا","ت","ن","م","ك","ط"],
        ["Shift", "ئ","ء","ؤ","ر","لا","ى","ة","و","ز", "Backspace"],
        ["Sym", "Lang", "Emoji", "Space", ".", "،", "Enter"],
    ],
    "he": [
        ["ק","ר","א","ט","ו","ן","ם","פ"],
        ["ש","ד","ג","כ","ע","י","ח","ל","ך","ף"],
        ["Shift", "ז","ס","ב","ה","נ","מ","צ","ת", "Backspace"],
        ["Sym", "Lang", "Emoji", "Space", ".", ",", "Enter"],
    ],
    # fallback: en layout
}

def _get_letter_rows(lang: str) -> List[List[str]]:
    return _LAYOUT_LETTERS.get(lang, _LAYOUT_LETTERS["en"])


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN KEYBOARD WIDGET
# ═══════════════════════════════════════════════════════════════════════════
class ModernOnScreenKeyboard(QWidget):
    """
    Gboard-inspired floating keyboard.
    Draggable: press & drag anywhere on the background pill.
    """

    onKeyHover        = Signal(dict)
    onKeyPress        = Signal(dict)
    onKeyHold         = Signal(dict)
    suggestionsChanged= Signal(list)

    # ── internal panels ────────────────────────────────────────────────────
    _PANEL_MAIN     = "main"
    _PANEL_NUMBERS  = "numbers"
    _PANEL_SYMBOLS  = "symbols"
    _PANEL_EMOJI    = "emoji"
    _PANEL_SETTINGS = "settings"
    _PANEL_CLIPBOARD= "clipboard"

    def __init__(self, config: Optional[KeyboardConfig] = None):
        super().__init__()
        self.config    = config or KeyboardConfig()
        self._theme    = THEMES["dark"]

        # Language
        self._system_langs = _SYSTEM_LANGUAGES
        self._lang_idx     = 0
        self._language     = self._system_langs[0]
        self._predictor    = OfflinePredictionEngine(self._language)

        # Panel / layer
        self._panel = self._PANEL_MAIN

        # Emoji paging
        self._emoji_page = 0
        self._emoji_pages = self._build_emoji_pages()

        # Modifier states:  None | "once" | "lock"
        self._shift_state = None    # None / "once" / "lock"
        self._ctrl_on  = False
        self._alt_on   = False

        # Word tracking
        self._last_word    = ""
        self._current_word = ""
        self._suggestions: List[str] = []

        # Key data
        self._key_rects:  Dict[str, QRectF] = {}
        self._key_specs:  Dict[str, KeySpec] = {}
        self._progress:   Dict[str, float]   = {}
        self._flash:      Dict[str, float]   = {}

        # Dwell state
        self._hover_key:          Optional[str]                = None
        self._hover_hold_emitted: bool                         = False
        self._hover_started_at:   Optional[float]              = None
        self._hover_stable_since: Optional[float]              = None
        self._hover_last_pos:     Optional[Tuple[float, float]]= None
        self._last_fire_at:       float                        = 0.0

        # Swipe
        self._swipe_enabled = True
        self._swipe_letters: List[str] = []
        self._swipe_last_t  = 0.0

        # Scanning
        self._scanning_enabled  = False
        self._scanning_row_mode = True
        self._scan_row = 0
        self._scan_col = 0
        self._scan_rows: List[List[str]] = []
        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._scan_advance)

        # Plugins / hooks
        self._plugins: List[KeyboardPlugin] = []
        self._audio_hook:  Optional[Callable[[str], None]] = None
        self._haptic_hook: Optional[Callable[[str], None]] = None
        self._tts_hook:    Optional[Callable[[str], None]] = None

        # Clipboard
        self._clipboard = ClipboardHistory()
        self._clipboard.changed.connect(self._on_clipboard_changed)

        # Settings panel sliders
        self._settings_items: List[dict] = []
        self._settings_hover: Optional[int] = None

        # Dragging the window
        self._drag_start_pos: Optional[QPoint] = None
        self._did_drag:   bool          = False
        self._press_local: Optional[QPoint] = None

        # Misc
        self._suggestion_rects: List[QRectF] = []

        # Input
        self._input = self._build_injector()

        # Anim timer
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._on_anim)

        # Window flags: frameless, always on top, no focus steal
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        # NOT WindowTransparentForInput — we need mouse events for dragging
        # The overlay will feed us cursor via update_cursor()

        self._rebuild_layout()
        self.hide()

    # ───────────────────────── Public API ──────────────────────────────────

    def show_near(self, x: int, y: int) -> None:
        self._place_window(x, y)
        self.show(); self.raise_()
        self._anim.start()

    def hide(self) -> None:  # type: ignore[override]
        super().hide(); self._anim.stop()

    def contains(self, x: int, y: int) -> bool:
        # Return False intentionally — the keyboard is NOT a safe zone for the overlay.
        # The overlay must still be able to arm/click/drag while the keyboard is visible.
        # Dwell on keyboard keys is handled internally via update_cursor().
        return False

    def geometry_contains(self, x: int, y: int) -> bool:
        """True if the point is physically inside the keyboard window (for overlay ring hiding only)."""
        return self.isVisible() and self.geometry().contains(x, y)

    def update_cursor(self, x: int, y: int) -> None:
        if not self.isVisible(): return
        now    = time.monotonic()
        key_id = self._hit_test_global(x, y)

        if key_id is None and self._swipe_enabled:
            if self._swipe_letters and (now - self._swipe_last_t) > self.config.swipe_commit_delay:
                self._commit_swipe_path()

        if key_id != self._hover_key:
            self._set_hover_key(key_id, now)
        if key_id is None: return
        if self._scanning_enabled: return
        self._update_dwell(key_id, x, y, now)

    def uses_external_dwell(self) -> bool: return False

    def get_key_bounds(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        gx = self.geometry().x(); gy = self.geometry().y()
        for kid, rect in self._key_rects.items():
            spec = self._key_specs[kid]
            out[kid] = {
                "label": spec.label, "kind": spec.kind,
                "rect": (int(gx+rect.x()), int(gy+rect.y()),
                         int(rect.width()), int(rect.height())),
                "language": self._language, "panel": self._panel,
            }
        return out

    def trigger_key(self, key_id: str) -> bool:
        if key_id not in self._key_specs: return False
        self._activate_key(key_id); return True

    def set_language(self, language: str) -> None:
        if language not in self._system_langs:
            self._system_langs.append(language)
        self._language = language
        self._predictor.set_language(language)
        self._rebuild_layout()

    def set_theme(self, theme_name: str, custom: Optional[GboardTheme] = None) -> None:
        if custom: self._theme = custom
        elif theme_name in THEMES: self._theme = THEMES[theme_name]
        self.update()

    def set_scale(self, scale: float) -> None:
        self.config.scale = min(max(scale, 0.6), 2.2)
        self._rebuild_layout()

    def set_dwell_profile(self, dwell_time: float, move_threshold: float, predwell_delay: float) -> None:
        self.config.dwell_time    = max(0.15, dwell_time)
        self.config.move_threshold= max(1.0, move_threshold)
        self.config.predwell_delay= max(0.0, predwell_delay)

    def set_scanning_mode(self, enabled: bool, interval_ms: Optional[int] = None) -> None:
        self._scanning_enabled  = enabled
        self._scanning_row_mode = True
        self._scan_row = self._scan_col = 0
        if interval_ms: self.config.scanning_interval_ms = max(180, interval_ms)
        if enabled: self._scan_timer.start(self.config.scanning_interval_ms)
        else: self._scan_timer.stop()
        self.update()

    def scan_select(self) -> None:
        if not self._scanning_enabled or not self._scan_rows: return
        row = self._scan_rows[self._scan_row]
        if not row: return
        if self._scanning_row_mode:
            self._scanning_row_mode = False; self._scan_col = 0
            self._scan_timer.start(self.config.scan_column_interval_ms)
        else:
            kid = row[min(self._scan_col, len(row)-1)]
            self._activate_key(kid)
            self._scanning_row_mode = True; self._scan_col = 0
            self._scan_timer.start(self.config.scanning_interval_ms)

    def register_plugin(self, plugin: KeyboardPlugin) -> None:
        self._plugins.append(plugin)
        if hasattr(plugin, "on_registered"): plugin.on_registered(self)

    def set_feedback_hooks(self,
        audio: Optional[Callable[[str], None]] = None,
        haptic: Optional[Callable[[str], None]] = None,
        tts: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._audio_hook = audio; self._haptic_hook = haptic; self._tts_hook = tts

    # ─────────────── Qt mouse events for window dragging ──────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            local = event.position().toPoint()
            self._press_local    = local
            self._did_drag       = False
            hit = self._hit_test_local(local.x(), local.y())
            # Start potential drag from background area (no key hit) OR settings/clipboard panels
            self._drag_start_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start_pos and (event.buttons() & Qt.LeftButton):
            offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft() - self._drag_start_pos
            if offset.manhattanLength() > 6:
                self._did_drag = True
            if self._did_drag:
                self.move(event.globalPosition().toPoint() - self._drag_start_pos)
        # Always update settings hover on mouse move
        if self._panel == self._PANEL_SETTINGS:
            local = event.position().toPoint()
            self._update_settings_hover(local.x(), local.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self._did_drag:
            local = getattr(self, "_press_local", event.position().toPoint())
            hit   = self._hit_test_local(local.x(), local.y())
            if hit:
                self._flash[hit] = 1.0
                self._activate_key(hit)
            elif self._panel == self._PANEL_SETTINGS:
                self._handle_settings_click(local.x(), local.y())
            elif self._panel == self._PANEL_CLIPBOARD:
                self._handle_clipboard_click(local.x(), local.y())
        self._drag_start_pos = None
        self._did_drag       = False
        super().mouseReleaseEvent(event)

    # ─────────────────────── Internal mechanics ────────────────────────────

    def _build_injector(self) -> BaseInputInjector:
        try:
            import pynput  # noqa: F401
            return PynputInputInjector()
        except Exception:
            return NullInputInjector()

    def _place_window(self, x: int, y: int) -> None:
        scr = QApplication.primaryScreen().geometry()
        m   = 12
        px  = min(x + 20, scr.right()  - self.width()  - m)
        py  = min(y + 20, scr.bottom() - self.height() - m)
        px  = max(px, scr.left() + m)
        py  = max(py, scr.top()  + m)
        self.move(int(px), int(py))

    # ─── Layout ────────────────────────────────────────────────────────────

    def _rebuild_layout(self) -> None:
        self._key_specs.clear()
        self._key_rects.clear()
        self._scan_rows.clear()

        sc   = self.config.scale
        kw   = self.config.key_w   * sc
        kh   = self.config.key_h   * sc
        gap  = self.config.key_gap * sc
        pad  = 10 * sc
        cr   = self.config.corner_radius

        is_rtl  = self._language in _RTL_LANGUAGES
        show_sg = self.config.autocomplete_enabled and self.config.suggestion_count > 0
        sg_h    = kh * 0.72 if show_sg else 0.0

        # -- build rows
        rows = self._panel_rows()

        y     = pad + sg_h + (gap if show_sg else 0.0)
        max_w = 0.0

        for row in rows:
            specs = [self._token_to_spec(tok) for tok in row]
            # RTL: mirror row visually
            if is_rtl: specs = list(reversed(specs))
            total_u = sum(s.width_units for s in specs)
            row_w   = total_u * kw + max(0, len(specs)-1) * gap
            max_w   = max(max_w, row_w)

            x = pad
            row_ids: List[str] = []
            for spec in specs:
                w    = spec.width_units * kw
                rect = QRectF(x, y, w, kh)
                self._key_specs[spec.key_id] = spec
                self._key_rects[spec.key_id] = rect
                row_ids.append(spec.key_id)
                x += w + gap
            self._scan_rows.append(row_ids)
            y += kh + gap

        panel_w = max_w + pad * 2
        panel_h = y + pad

        # Settings / emoji panels may have different heights; already computed via rows.

        # -- suggestion strip rects
        self._suggestion_rects = []
        if show_sg:
            sw = (panel_w - pad * 2) / max(1, self.config.suggestion_count)
            for i in range(self.config.suggestion_count):
                self._suggestion_rects.append(
                    QRectF(pad + i * sw, pad, sw, sg_h)
                )
                kid = f"sugg:{i}"
                self._key_specs[kid] = KeySpec(kid, "", kind="suggestion")
                self._key_rects[kid] = self._suggestion_rects[i]

        # Normal panel size
        self.setFixedSize(int(panel_w), int(panel_h))

        # Settings / clipboard panels: compute their height directly
        if self._panel == self._PANEL_SETTINGS:
            if not self._settings_items:
                self._build_settings_panel()
            n_items  = len(self._settings_items)
            header_h = int(38 * sc)
            row_h    = int(44 * sc)
            sep      = int(4 * sc)
            theme_row= row_h     # 1 row for theme
            lang_row = row_h     # 1 row for languages
            panel_h  = int(header_h + int(pad*0.5) + theme_row + n_items*(row_h+sep) + lang_row + pad*2)
            panel_w  = max(int(560 * sc), int(500 * sc))
            self.setFixedSize(panel_w, panel_h)
            self.update()
            return

        if self._panel == self._PANEL_CLIPBOARD:
            n = min(8, len(self._clipboard.items()))
            panel_h = int(40*sc + n*int(42*sc) + int(pad*2))
            panel_w = max(int(360 * sc), int(300 * sc))
            self.setFixedSize(panel_w, max(panel_h, int(120*sc)))
            self.update()
            return
        self._progress = {k: 0.0 for k in self._key_rects}
        self._flash    = {k: 0.0 for k in self._key_rects}
        self._refresh_suggestions()
        self.update()

    def _panel_rows(self) -> List[List[str]]:
        if self._panel == self._PANEL_NUMBERS:
            return [
                list("1234567890"),
                ["@","#","$","_","&","-","+","(",")","/"],
                ["Sym", "*", '"', "'", ";", ":", "!", "?", "Backspace"],
                ["ABC", "Lang", "Emoji", "Space", ".", ",", "Enter"],
            ]
        if self._panel == self._PANEL_SYMBOLS:
            return [
                ["[","]","{","}","#","%","^","*","+","="],
                ["_","\\","|","~","<",">","€","£","¥","•"],
                ["123", ".", ",", "?", "!", "'", '"', "Emoji", "Backspace"],
                ["ABC", "@", "/", "Space", "Settings", "Enter"],
            ]
        if self._panel == self._PANEL_EMOJI:
            page = self._emoji_pages[self._emoji_page % len(self._emoji_pages)]
            return page + [["ABC", "123", "◀", "▶", "Space", "Enter"]]
        if self._panel == self._PANEL_SETTINGS:
            return []   # handled separately in paint
        if self._panel == self._PANEL_CLIPBOARD:
            return []
        # main letters
        return _get_letter_rows(self._language)

    def _build_emoji_pages(self) -> List[List[List[str]]]:
        emojis = [
            ["😀","😁","😂","🤣","😊","😍","😘","😎","🤔","🥹"],
            ["😭","😡","🥳","🤯","😴","🤮","🥸","🫠","🫡","🤩"],
            ["👍","👎","👏","🙏","🤝","🫶","❤️","💔","🔥","⭐"],
            ["🎉","🎊","🎈","🎁","🏆","🥇","💡","🔑","📌","✅"],
            ["🌍","🌙","☀️","⚡","🌈","❄️","🍀","🌸","🎵","🎶"],
        ]
        # Group into pages of 2 rows × 10
        pages: List[List[List[str]]] = []
        for i in range(0, len(emojis), 2):
            pages.append(emojis[i:i+2])
        return pages

    def _token_to_spec(self, token: str) -> KeySpec:
        t = token
        # ── special tokens ──────────────────────────────────────────────
        specials: Dict[str, KeySpec] = {
            "Space":    KeySpec("space",          "Space",    "space",    4.0),
            "Backspace":KeySpec("backspace",       "⌫",        "backspace",1.8),
            "Shift":    KeySpec("shift",           "⇧",        "modifier", 1.8),
            "Ctrl":     KeySpec("ctrl",            "Ctrl",     "modifier", 1.6),
            "Alt":      KeySpec("alt",             "Alt",      "modifier", 1.6),
            "Enter":    KeySpec("enter",           "↵",        "enter",    2.0),
            "123":      KeySpec("to_numbers",      "123",      "layer",    1.6),
            "Num":      KeySpec("to_numbers",      "123",      "layer",    1.6),
            "Sym":      KeySpec("to_symbols",      "#+=",      "layer",    1.6),
            "ABC":      KeySpec("to_letters",      "ABC",      "layer",    1.6),
            "Emoji":    KeySpec("to_emoji",        "😊",        "layer",    1.4),
            "Settings": KeySpec("to_settings",     "⚙",        "layer",    1.4),
            "Lang":     KeySpec("lang",            self._lang_label(), "language", 1.5),
            "◀":        KeySpec("emoji_prev",      "◀",        "system",   1.2),
            "▶":        KeySpec("emoji_next",      "▶",        "system",   1.2),
            "Clip":     KeySpec("clipboard",       "📋",        "system",   1.4),
            "Copy":     KeySpec("copy",            "Copy",     "system",   1.6),
            "Paste":    KeySpec("paste",           "Paste",    "system",   1.6),
            "Undo":     KeySpec("undo",            "Undo",     "system",   1.6),
            "Redo":     KeySpec("redo",            "Redo",     "system",   1.6),
        }
        if t in specials: return specials[t]
        return KeySpec(f"c_{t}", t, "char", 1.0, payload=t)

    def _lang_label(self) -> str:
        lang = self._language.upper()
        if self._language in _RTL_LANGUAGES:
            icons = {"ar": "ع", "he": "א", "fa": "ف", "ur": "ا"}
            return icons.get(self._language, lang[:2])
        return lang[:2]

    # ─── Suggestions ───────────────────────────────────────────────────────

    def _refresh_suggestions(self) -> None:
        if not self.config.autocomplete_enabled or self.config.suggestion_count <= 0:
            self._suggestions = []; self.suggestionsChanged.emit([]); self.update(); return
        self._suggestions = self._predictor.suggest(
            prefix=self._current_word, last_word=self._last_word,
            limit=self.config.suggestion_count,
        )
        # Update labels in key_specs
        for i, rect in enumerate(self._suggestion_rects):
            kid  = f"sugg:{i}"
            spec = self._key_specs.get(kid)
            if spec: spec.label = self._suggestions[i] if i < len(self._suggestions) else ""
        self.suggestionsChanged.emit(list(self._suggestions))
        self.update()

    # ─── Hit testing ───────────────────────────────────────────────────────

    def _hit_test_global(self, gx: float, gy: float) -> Optional[str]:
        lx = gx - self.geometry().x()
        ly = gy - self.geometry().y()
        return self._hit_test_local(lx, ly)

    def _hit_test_local(self, lx: float, ly: float) -> Optional[str]:
        pt = QPointF(lx, ly)
        for kid, rect in self._key_rects.items():
            if rect.contains(pt): return kid
        return None

    # ─── Hover / dwell ─────────────────────────────────────────────────────

    def _set_hover_key(self, key_id: Optional[str], now: float) -> None:
        self._hover_key         = key_id
        self._hover_started_at  = now if key_id else None
        self._hover_stable_since= now if key_id else None
        self._hover_last_pos    = None
        self._hover_hold_emitted= False
        for k in self._progress:
            if k != key_id: self._progress[k] = 0.0
        if key_id:
            meta = self._key_meta(key_id)
            self.onKeyHover.emit(meta)
            self._plugin_dispatch("on_key_hover", meta)
            # Swipe tracking
            spec = self._key_specs.get(key_id)
            if spec and spec.kind == "char" and spec.label.isalpha():
                letter = spec.label.lower()
                if self._swipe_letters and (now - self._swipe_last_t) > self.config.swipe_link_window:
                    self._swipe_letters = []
                if not self._swipe_letters or self._swipe_letters[-1] != letter:
                    self._swipe_letters.append(letter)
                self._swipe_last_t = now
        self.update()

    def _update_dwell(self, key_id: str, x: float, y: float, now: float) -> None:
        if self._hover_last_pos is None:
            self._hover_last_pos = (x, y); return
        dx = x - self._hover_last_pos[0]; dy = y - self._hover_last_pos[1]
        self._hover_last_pos = (x, y)
        if math.hypot(dx, dy) > self.config.move_threshold:
            self._hover_started_at = now; self._hover_stable_since = now
            self._progress[key_id] = 0.0; self._hover_hold_emitted = False
            self.update(); return
        if self._hover_stable_since is None: self._hover_stable_since = now; return
        if now - self._hover_stable_since < self.config.predwell_delay: return
        if self._hover_started_at is None: self._hover_started_at = now
        elapsed = now - self._hover_started_at
        prog = min(1.0, elapsed / max(0.08, self.config.dwell_time))
        self._progress[key_id] = prog
        if not self._hover_hold_emitted and prog >= 0.55:
            self._hover_hold_emitted = True
            meta = self._key_meta(key_id)
            self.onKeyHold.emit(meta); self._plugin_dispatch("on_key_hold", meta)
        if prog >= 1.0 and now - self._last_fire_at >= self.config.cooldown:
            self._last_fire_at = now; self._progress[key_id] = 0.0
            self._activate_key(key_id)
            self._hover_started_at = now; self._hover_hold_emitted = False
        self.update()

    # ─── Key activation ────────────────────────────────────────────────────

    def _activate_key(self, key_id: str) -> None:
        spec = self._key_specs.get(key_id)
        if not spec: return
        self._flash[key_id] = 1.0
        meta = self._key_meta(key_id)
        self.onKeyPress.emit(meta)
        self._plugin_dispatch("on_key_press", meta)
        self._feedback("key_press")

        kind = spec.kind

        if key_id.startswith("sugg:"):
            idx = int(key_id.split(":")[1])
            if idx < len(self._suggestions): self._commit_suggestion(self._suggestions[idx])
            return

        if kind == "char":          self._type_char(spec.payload or spec.label); return
        if kind == "space":         self._commit_space(); return
        if kind == "backspace":     self._handle_backspace(); return
        if kind == "enter":         self._commit_enter(); return
        if kind == "modifier":      self._toggle_modifier(key_id); return
        if kind == "layer":         self._switch_layer(key_id); return
        if kind == "language":      self._rotate_language(); return
        if kind == "system":        self._handle_system(key_id); return

    def _commit_suggestion(self, word: str) -> None:
        if not word: return
        if self._current_word and word.lower().startswith(self._current_word.lower()):
            suffix = word[len(self._current_word):]
            if suffix: self._input.type_text(suffix)
        else:
            self._input.type_text(word)
        self._input.tap("space")
        self._predictor.learn_word(word, self._last_word)
        self._last_word = word; self._current_word = ""
        self._refresh_suggestions()

    def _type_char(self, char: str) -> None:
        if len(char) == 1 and char.isalpha():
            should_upper = self._shift_state is not None
            out = char.upper() if should_upper else char.lower()
            if self._ctrl_on:
                self._input.chord([self._ctrl_key()], out.lower())
            elif self._alt_on:
                self._input.chord(["alt"], out.lower())
            elif should_upper:
                self._input.chord(["shift"], char.lower())
            else:
                self._input.type_text(out)
            self._current_word += out
            if self._shift_state == "once":
                self._shift_state = None
        else:
            self._input.type_text(char)
            if char not in "'-": self._current_word = ""
            else: self._current_word += char
        self._refresh_suggestions()

    def _commit_space(self) -> None:
        self._input.tap("space")
        committed = self._current_word.strip()
        if committed: self._predictor.learn_word(committed, self._last_word); self._last_word = committed
        self._current_word = ""
        if self._shift_state == "once": self._shift_state = None
        self._refresh_suggestions()

    def _commit_enter(self) -> None:
        self._input.tap("enter")
        committed = self._current_word.strip()
        if committed: self._predictor.learn_word(committed, self._last_word); self._last_word = committed
        self._current_word = ""
        if self._shift_state == "once": self._shift_state = None
        self._refresh_suggestions()

    def _handle_backspace(self) -> None:
        self._input.tap("backspace")
        if self._current_word: self._current_word = self._current_word[:-1]
        self._refresh_suggestions()

    def _toggle_modifier(self, key_id: str) -> None:
        """
        Shift cycles:  None → "once" → "lock" → None
        Second hover on an ACTIVE modifier CANCELS it (toggle off).
        Ctrl / Alt: simple toggle; second hover turns off.
        """
        if key_id == "shift":
            if   self._shift_state is None:   self._shift_state = "once"
            elif self._shift_state == "once":  self._shift_state = "lock"
            else:                              self._shift_state = None
        elif key_id == "ctrl":
            self._ctrl_on = not self._ctrl_on
            self._input.press_modifier(self._ctrl_key(), self._ctrl_on)
        elif key_id == "alt":
            self._alt_on = not self._alt_on
            self._input.press_modifier("alt", self._alt_on)
        # Update lang key label (may have changed RTL logic)
        if "lang" in self._key_specs:
            self._key_specs["lang"].label = self._lang_label()
        self.update()

    def _switch_layer(self, key_id: str) -> None:
        mapping = {
            "to_numbers":  self._PANEL_NUMBERS,
            "to_symbols":  self._PANEL_SYMBOLS,
            "to_letters":  self._PANEL_MAIN,
            "to_emoji":    self._PANEL_EMOJI,
            "to_settings": self._PANEL_SETTINGS,
        }
        self._panel = mapping.get(key_id, self._PANEL_MAIN)
        if self._panel == self._PANEL_SETTINGS:
            self._build_settings_panel()
        self._rebuild_layout()

    def _rotate_language(self) -> None:
        self._lang_idx  = (self._lang_idx + 1) % len(self._system_langs)
        self._language  = self._system_langs[self._lang_idx]
        self._predictor.set_language(self._language)
        self._rebuild_layout()
        self._feedback("language")

    def _handle_system(self, key_id: str) -> None:
        ctrl = self._ctrl_key()
        if key_id == "copy":    self._input.chord([ctrl], "c"); return
        if key_id == "paste":   self._input.chord([ctrl], "v"); return
        if key_id == "undo":    self._input.chord([ctrl], "z"); return
        if key_id == "redo":
            if sys.platform == "darwin": self._input.chord([ctrl, "shift"], "z")
            else: self._input.chord([ctrl], "y")
            return
        if key_id == "emoji_next":
            self._emoji_page = (self._emoji_page + 1) % len(self._emoji_pages)
            self._rebuild_layout(); return
        if key_id == "emoji_prev":
            self._emoji_page = (self._emoji_page - 1) % len(self._emoji_pages)
            self._rebuild_layout(); return
        if key_id == "clipboard":
            self._panel = self._PANEL_CLIPBOARD; self._rebuild_layout(); return

    # ─── Settings panel ────────────────────────────────────────────────────

    def _build_settings_panel(self) -> None:
        """
        Populate _settings_items.  Each item: {label, value, min, max, step, attr}
        Rendered as rows in paintEvent and hit-tested separately.
        """
        self._settings_items = [
            {"label": "Dwell time",    "attr": "dwell_time",   "min": 0.2,  "max": 3.0, "step": 0.05},
            {"label": "Move threshold","attr": "move_threshold","min": 1.0,  "max": 20.0,"step": 0.5},
            {"label": "Key width",     "attr": "key_w",         "min": 32,   "max": 72,  "step": 2},
            {"label": "Key height",    "attr": "key_h",         "min": 36,   "max": 80,  "step": 2},
            {"label": "Key gap",       "attr": "key_gap",       "min": 2,    "max": 18,  "step": 1},
            {"label": "Scale",         "attr": "scale",         "min": 0.6,  "max": 2.2, "step": 0.05},
            {"label": "Autocomplete",  "attr": "autocomplete_enabled", "type": "bool"},
            {"label": "Suggestions",   "attr": "suggestion_count","min": 0, "max": 5, "step": 1},
        ]

    # ─── Swipe / clipboard ─────────────────────────────────────────────────

    def _commit_swipe_path(self) -> None:
        letters = list(self._swipe_letters); self._swipe_letters = []
        if len(letters) < 3: return
        word = self._predictor.decode_swipe(letters)
        if not word: return
        self._input.type_text(word); self._input.tap("space")
        self._predictor.learn_word(word, self._last_word)
        self._last_word = word; self._current_word = ""
        self._feedback("swipe"); self._refresh_suggestions()

    def _on_clipboard_changed(self, _items: list) -> None:
        if self._panel == self._PANEL_CLIPBOARD: self._rebuild_layout()

    # ─── Scanning ──────────────────────────────────────────────────────────

    def _scan_advance(self) -> None:
        if not self._scanning_enabled or not self._scan_rows: return
        if self._scanning_row_mode: self._scan_row = (self._scan_row + 1) % len(self._scan_rows)
        else:
            row = self._scan_rows[self._scan_row]
            if row: self._scan_col = (self._scan_col + 1) % len(row)
        self.update()

    # ─── Animation ─────────────────────────────────────────────────────────

    def _on_anim(self) -> None:
        changed = False
        for k, v in list(self._flash.items()):
            if v > 0:
                self._flash[k] = max(0.0, v - 0.08)
                changed = True
        if changed: self.update()

    # ─── Helpers ───────────────────────────────────────────────────────────

    def _ctrl_key(self) -> str:
        return "cmd" if sys.platform == "darwin" else "ctrl"

    def _key_meta(self, key_id: str) -> dict:
        spec = self._key_specs[key_id]; rect = self._key_rects[key_id]
        gx = self.geometry().x() + int(rect.x()); gy = self.geometry().y() + int(rect.y())
        return {
            "id": key_id, "label": spec.label, "kind": spec.kind,
            "rect": (gx, gy, int(rect.width()), int(rect.height())),
            "language": self._language, "panel": self._panel,
            "modifiers": {
                "shift": self._shift_state, "caps": self._shift_state == "lock",
                "ctrl": self._ctrl_on, "alt": self._alt_on,
            },
        }

    def _plugin_dispatch(self, method: str, meta: dict) -> None:
        for p in self._plugins:
            fn = getattr(p, method, None)
            if callable(fn):
                try: fn(meta)
                except Exception as e: print(f"[KB] plugin {method}: {e}")

    def _feedback(self, event: str) -> None:
        for h in (self._audio_hook, self._haptic_hook, self._tts_hook):
            if h:
                try: h(event)
                except Exception: pass

    def _update_settings_hover(self, lx: float, ly: float) -> None:
        """Update hover state for settings panel widgets."""
        # We use a hit-test approach mirroring _paint_settings_panel geometry.
        # Recompute rects cheaply.
        hit = self._settings_hit_test(lx, ly)
        if hit != self._settings_hover:
            self._settings_hover = hit
            self.update()

    def _settings_hit_test(self, lx: float, ly: float) -> Optional[int]:
        sc  = self.config.scale
        pad = int(12 * sc)
        W   = float(self.width())
        header_h = int(38 * sc)
        row_h    = int(44 * sc)
        themes   = list(THEMES.keys())

        # Back button: id=-1
        back_rect = QRectF(W - pad - 60*sc, 6*sc, 56*sc, 26*sc)
        if back_rect.contains(lx, ly): return -1

        # Theme buttons: id=100+ti
        theme_y = header_h + int(pad * 0.5)
        btn_w = int(60*sc); btn_h = int(28*sc); btn_gap = int(6*sc)
        bx = pad + int(110*sc)
        by = int(theme_y + (row_h - btn_h) / 2)
        for ti in range(len(themes)):
            br = QRectF(bx, by, btn_w, btn_h)
            if br.contains(lx, ly): return 100 + ti
            bx += btn_w + btn_gap

        y = theme_y + row_h
        # Slider ± buttons: id=200 + idx*2 + (0=dec, 1=inc)
        for idx, item in enumerate(self._settings_items):
            if item.get("type") != "bool":
                bar_x = int(W - pad - 180*sc)
                pm_w = int(26*sc); pm_h = int(26*sc)
                dec_r = QRectF(bar_x - pm_w - 6*sc, y + (row_h-pm_h)/2, pm_w, pm_h)
                inc_r = QRectF(bar_x + int(170*sc) + 6*sc, y + (row_h-pm_h)/2, pm_w, pm_h)
                if dec_r.contains(lx, ly): return 200 + idx*2
                if inc_r.contains(lx, ly): return 200 + idx*2 + 1
                # Slider track itself: id = 300+idx
                bar_rect = QRectF(bar_x, y + row_h/2 - 3*sc, int(170*sc), int(6*sc))
                if bar_rect.adjusted(-4,-10,4,10).contains(lx, ly): return 300 + idx
            else:
                sw_x = int(W - pad - 48*sc); sw_y = int(y + (row_h - 22*sc)/2)
                sw_r = QRectF(sw_x, sw_y, int(44*sc), int(22*sc))
                if sw_r.contains(lx, ly): return 200 + idx*2

            y += row_h + int(4*sc)

        # Language buttons: id=500+li
        lang_y = y
        bx2 = pad + int(160*sc)
        by2 = int(lang_y + (row_h - int(28*sc))/2)
        for li in range(len(self._system_langs)):
            lb_r = QRectF(bx2, by2, int(46*sc), int(28*sc))
            if lb_r.contains(lx, ly): return 500 + li
            bx2 += int(46*sc) + int(6*sc)

        # Slider row hover: id = idx
        y2 = theme_y + row_h
        for idx in range(len(self._settings_items)):
            row_rect = QRectF(pad, y2, W - pad*2, row_h)
            if row_rect.contains(lx, ly): return idx
            y2 += row_h + int(4*sc)

        return None

    def _handle_settings_click(self, lx: float, ly: float) -> None:
        hit = self._settings_hit_test(lx, ly)
        if hit is None: return
        sc = self.config.scale
        W  = float(self.width())
        pad= int(12*sc)

        if hit == -1:
            # Back
            self._panel = self._PANEL_MAIN; self._rebuild_layout(); return

        if 100 <= hit < 100 + len(THEMES):
            name = list(THEMES.keys())[hit - 100]
            self.set_theme(name); return

        if 200 <= hit < 200 + len(self._settings_items) * 2:
            rel  = hit - 200
            idx  = rel // 2
            item = self._settings_items[idx]
            attr = item["attr"]
            is_dec = (rel % 2 == 0)
            if item.get("type") == "bool":
                setattr(self.config, attr, not getattr(self.config, attr))
            else:
                val  = getattr(self.config, attr)
                step = item["step"]
                if is_dec: val = max(item["min"], val - step)
                else:       val = min(item["max"], val + step)
                val = round(val, 4)
                setattr(self.config, attr, val)
                if attr in ("key_w", "key_h", "key_gap", "scale"):
                    self._rebuild_layout(); return
            self.update(); return

        if 300 <= hit < 300 + len(self._settings_items):
            # Click on slider track — set value based on X position
            idx  = hit - 300
            item = self._settings_items[idx]
            attr = item["attr"]
            bar_x = int(W - pad - 180*sc)
            bar_w = int(170*sc)
            frac  = min(1.0, max(0.0, (lx - bar_x) / bar_w))
            val   = item["min"] + frac * (item["max"] - item["min"])
            step  = item["step"]
            val   = round(round(val / step) * step, 4)
            setattr(self.config, attr, val)
            if attr in ("key_w", "key_h", "key_gap", "scale"):
                self._rebuild_layout(); return
            self.update(); return

        if 500 <= hit < 500 + len(self._system_langs):
            li = hit - 500
            self._lang_idx = li
            self._language = self._system_langs[li]
            self._predictor.set_language(self._language)
            self._panel = self._PANEL_MAIN
            self._rebuild_layout(); return

    def _handle_clipboard_click(self, lx: float, ly: float) -> None:
        sc  = self.config.scale
        pad = int(12 * sc)
        W   = float(self.width())
        # Back button
        back_rect = QRectF(W - pad - 60*sc, 6*sc, 56*sc, 26*sc)
        if back_rect.contains(lx, ly):
            self._panel = self._PANEL_MAIN; self._rebuild_layout(); return
        # Item rows
        item_h = int(42 * sc)
        y = int(40 * sc)
        items = self._clipboard.items()
        for idx, item in enumerate(items[:8]):
            ir = QRectF(pad, y, W - pad*2, item_h - int(4*sc))
            if ir.contains(lx, ly):
                self._input.type_text(item)
                self._panel = self._PANEL_MAIN; self._rebuild_layout(); return
            y += item_h
    # ═══════════════════════════════════════════════════════════════════════

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)

        W, H = float(self.width()), float(self.height())
        sc   = self.config.scale
        cr   = max(4, int(16 * sc))   # window corner radius
        th   = self._theme

        # ── Window shadow ────────────────────────────────────────────────
        for offset in range(8, 0, -1):
            sc_rect = QRectF(offset*0.5, offset, W - offset*0.5, H - offset*0.3)
            shadow_c = QColor(th.shadow)
            shadow_c.setAlpha(int(shadow_c.alpha() * offset / 8))
            p.setBrush(shadow_c); p.setPen(Qt.NoPen)
            p.drawRoundedRect(sc_rect, cr, cr)

        # ── Window background ────────────────────────────────────────────
        p.setBrush(th.win_bg); p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(0, 0, W, H), cr, cr)

        # ── Thin top accent line (Gboard style) ──────────────────────────
        accent = QColor(th.dwell_arc)
        accent.setAlpha(80)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(accent, 1.5))
        p.drawLine(QPointF(cr, 0.75), QPointF(W-cr, 0.75))

        # ── Settings panel (custom rendering) ────────────────────────────
        if self._panel == self._PANEL_SETTINGS:
            self._paint_settings_panel(p, W, H)
            p.end(); return

        # ── Clipboard panel ───────────────────────────────────────────────
        if self._panel == self._PANEL_CLIPBOARD:
            self._paint_clipboard_panel(p, W, H)
            p.end(); return

        # ── Suggestion strip ─────────────────────────────────────────────
        self._paint_suggestions(p)

        # ── Keys ─────────────────────────────────────────────────────────
        self._paint_keys(p)

        # ── Scanning overlay ─────────────────────────────────────────────
        if self._scanning_enabled:
            self._paint_scan_focus(p)

        # ── Status bar ───────────────────────────────────────────────────
        self._paint_statusbar(p, W, H)

        p.end()

    def _paint_suggestions(self, p: QPainter) -> None:
        if not self._suggestion_rects: return
        th = self._theme; sc = self.config.scale

        # Suggestion strip background
        strip_h = self._suggestion_rects[0].height() if self._suggestion_rects else 0
        strip_bg = QRectF(0, 0, float(self.width()), strip_h + self.config.key_gap * sc)
        p.setBrush(th.sugg_bg); p.setPen(Qt.NoPen)
        p.drawRect(strip_bg)

        fnt = QFont("Segoe UI", max(9, int(12 * sc)), QFont.Medium)
        p.setFont(fnt)

        for i, rect in enumerate(self._suggestion_rects):
            kid   = f"sugg:{i}"
            hover = self._hover_key == kid
            flash = self._flash.get(kid, 0.0)
            text  = self._suggestions[i] if i < len(self._suggestions) else ""

            if flash > 0:
                fill = QColor(th.key_active); fill.setAlpha(int(180 + flash * 70))
            elif hover:
                fill = QColor(th.key_hover)
            else:
                fill = QColor(th.sugg_bg)

            p.setBrush(fill); p.setPen(Qt.NoPen)
            p.drawRoundedRect(rect, 8, 8)

            # Divider between suggestions
            if i > 0:
                p.setPen(QPen(th.sugg_divider, 1))
                p.drawLine(QPointF(rect.left(), rect.top()+6),
                           QPointF(rect.left(), rect.bottom()-6))

            p.setPen(th.text_sugg if text else th.text_hint)
            p.drawText(rect, Qt.AlignCenter, text or "·")

    def _paint_keys(self, p: QPainter) -> None:
        th  = self._theme
        sc  = self.config.scale
        cr  = self.config.corner_radius

        fnt_lg = QFont("Segoe UI", max(10, int(14 * sc)), QFont.Medium)
        fnt_sm = QFont("Segoe UI", max(8,  int(10 * sc)), QFont.Medium)

        shift_on = self._shift_state is not None
        caps_on  = self._shift_state == "lock"

        for kid, rect in self._key_rects.items():
            if kid.startswith("sugg:"): continue
            spec    = self._key_specs[kid]
            hover   = self._hover_key == kid
            prog    = self._progress.get(kid, 0.0)
            flash   = self._flash.get(kid, 0.0)
            is_rtl  = self._language in _RTL_LANGUAGES

            # ── Key fill ─────────────────────────────────────────────────
            if spec.kind in {"modifier","layer","language","system","backspace","enter","space"}:
                fill = QColor(th.key_action)
            else:
                fill = QColor(th.key_normal)

            # Active modifier highlight
            mod_on = (
                (kid == "shift" and shift_on) or
                (kid == "ctrl"  and self._ctrl_on) or
                (kid == "alt"   and self._alt_on)
            )
            if mod_on: fill = QColor(th.key_mod_on)

            if hover:   fill = QColor(th.key_hover)
            if prog > 0:
                dwell_c = QColor(th.key_dwell)
                dwell_c.setAlpha(int(60 + prog * 180))
                # Blend dwell over current fill
                fill.setRed(  int(fill.red()  *(1-prog*0.4) + dwell_c.red()  *prog*0.4))
                fill.setGreen(int(fill.green()*(1-prog*0.4) + dwell_c.green()*prog*0.4))
                fill.setBlue( int(fill.blue() *(1-prog*0.4) + dwell_c.blue() *prog*0.4))
            if flash > 0:
                active_c = QColor(th.key_active)
                active_c.setAlpha(int(150 + flash*100))
                fill = active_c

            p.setBrush(fill); p.setPen(Qt.NoPen)
            p.drawRoundedRect(rect, cr, cr)

            # ── Dwell progress arc ────────────────────────────────────────
            if prog > 0:
                m    = 3.0
                arc_r = QRectF(rect.x()+m, rect.y()+m, rect.width()-m*2, rect.height()-m*2)
                pen   = QPen(th.dwell_arc, 2.0, Qt.SolidLine, Qt.RoundCap)
                p.setPen(pen); p.setBrush(Qt.NoBrush)
                p.drawArc(arc_r, 90*16, -int(prog*360*16))

            # ── Caps lock indicator ───────────────────────────────────────
            if kid == "shift" and caps_on:
                dot_c = QColor(th.text_key)
                p.setBrush(dot_c); p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(rect.center().x(), rect.bottom()-5), 2.5, 2.5)

            # ── Label ─────────────────────────────────────────────────────
            label = spec.label
            # uppercase display if shift active
            if spec.kind == "char" and len(label) == 1 and label.isalpha() and shift_on:
                label = label.upper()

            p.setPen(th.text_key)
            use_small = len(label) > 2 or spec.kind in {"layer","system","language"}
            p.setFont(fnt_sm if use_small else fnt_lg)

            # Align: for RTL keys nudge right
            alignment = Qt.AlignCenter
            p.drawText(rect, alignment, label)

            # ── Small secondary hint (number row position) ────────────────
            if spec.kind == "char" and len(label) == 1 and not label.isdigit():
                pass   # Could add number hints here if desired

        # ── Scan overlay ──────────────────────────────────────────────────
        if self._scanning_enabled: self._paint_scan_focus(p)

    def _paint_scan_focus(self, p: QPainter) -> None:
        if not self._scan_rows: return
        p.setPen(QPen(QColor(255, 200, 60, 255), 2.4))
        p.setBrush(Qt.NoBrush)
        if self._scanning_row_mode:
            row = self._scan_rows[self._scan_row]
            rects = [self._key_rects[k] for k in row if k in self._key_rects]
            if not rects: return
            l = min(r.left() for r in rects); t = min(r.top() for r in rects)
            r = max(r.right() for r in rects); b = max(r.bottom() for r in rects)
            p.drawRoundedRect(QRectF(l-3, t-3, r-l+6, b-t+6), 12, 12)
        else:
            row = self._scan_rows[self._scan_row]
            if not row: return
            kid = row[min(self._scan_col, len(row)-1)]
            rect = self._key_rects.get(kid)
            if rect: p.drawRoundedRect(rect.adjusted(-3,-3,3,3), 12, 12)

    def _paint_settings_panel(self, p: QPainter, W: float, H: float) -> None:
        th   = self._theme
        sc   = self.config.scale
        pad  = int(12 * sc)
        row_h= int(44 * sc)
        fnt  = QFont("Segoe UI", max(9, int(11 * sc)), QFont.Medium)
        fnt_val = QFont("Segoe UI", max(8, int(10 * sc)))
        p.setFont(fnt)

        # Header
        header_h = int(38 * sc)
        p.setPen(QColor(th.text_key))
        p.setFont(QFont("Segoe UI", max(10, int(13 * sc)), QFont.Bold))
        p.drawText(QRectF(pad, 0, W - pad*2, header_h), Qt.AlignVCenter | Qt.AlignLeft, "⚙  Keyboard Settings")

        # Back button
        back_rect = QRectF(W - pad - 60*sc, 6*sc, 56*sc, 26*sc)
        hover_back= (self._settings_hover == -1)
        p.setBrush(QColor(th.key_action) if not hover_back else QColor(th.key_hover))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(back_rect, 8, 8)
        p.setPen(QColor(th.text_key))
        p.setFont(fnt)
        p.drawText(back_rect, Qt.AlignCenter, "← Back")

        y = header_h + pad * 0.5

        # Theme row
        theme_y  = y
        theme_h  = row_h
        p.setFont(fnt)
        p.setPen(QColor(th.text_key))
        p.drawText(QRectF(pad, theme_y, 100*sc, theme_h), Qt.AlignVCenter | Qt.AlignLeft, "Theme")
        themes = list(THEMES.keys())
        btn_w = int(60 * sc); btn_h = int(28 * sc); btn_gap = int(6 * sc)
        bx = pad + int(110 * sc)
        by = int(theme_y + (theme_h - btn_h) / 2)
        for ti, tname in enumerate(themes):
            btn_rect = QRectF(bx, by, btn_w, btn_h)
            is_cur   = (tname == self._theme.name)
            hover_t  = (self._settings_hover == 100 + ti)
            fill     = QColor(th.key_mod_on) if is_cur else (
                        QColor(th.key_hover) if hover_t else QColor(th.key_action))
            p.setBrush(fill); p.setPen(Qt.NoPen)
            p.drawRoundedRect(btn_rect, 7, 7)
            p.setPen(QColor(th.text_key))
            p.setFont(fnt_val)
            p.drawText(btn_rect, Qt.AlignCenter, tname.replace("_"," ").title())
            bx += btn_w + btn_gap
        y += theme_h

        # Slider rows
        for idx, item in enumerate(self._settings_items):
            attr  = item["attr"]
            value = getattr(self.config, attr)
            is_bool = item.get("type") == "bool"
            row_rect = QRectF(pad, y, W - pad*2, row_h)
            hover_row= (self._settings_hover == idx)
            if hover_row:
                p.setBrush(QColor(th.key_hover)); p.setPen(Qt.NoPen)
                p.drawRoundedRect(row_rect.adjusted(-2,2,2,-2), 8, 8)

            p.setFont(fnt); p.setPen(QColor(th.text_key))
            p.drawText(QRectF(pad, y, 160*sc, row_h), Qt.AlignVCenter | Qt.AlignLeft, item["label"])

            if is_bool:
                # Toggle switch
                sw_x   = int(W - pad - 48*sc)
                sw_y   = int(y + (row_h - 22*sc)/2)
                sw_w, sw_h = int(44*sc), int(22*sc)
                sw_rect = QRectF(sw_x, sw_y, sw_w, sw_h)
                on = bool(value)
                p.setBrush(QColor(th.key_mod_on) if on else QColor(th.key_action))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(sw_rect, sw_h//2, sw_h//2)
                knob_x = (sw_x + sw_w - sw_h + 2) if on else (sw_x + 2)
                p.setBrush(QColor(th.text_key))
                p.drawEllipse(QPointF(knob_x + (sw_h-4)*sc/2, sw_y + sw_h/2), (sw_h/2-2), (sw_h/2-2))
            else:
                mn, mx, step = item["min"], item["max"], item["step"]
                frac = (value - mn) / max(0.001, mx - mn)
                bar_x = int(W - pad - 180*sc)
                bar_y = int(y + row_h/2 - 3*sc)
                bar_w = int(170*sc); bar_h = int(6*sc)
                # Track
                p.setBrush(QColor(th.key_action)); p.setPen(Qt.NoPen)
                p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), bar_h//2, bar_h//2)
                # Fill
                filled_w = max(bar_h, int(frac * bar_w))
                p.setBrush(QColor(th.dwell_arc))
                p.drawRoundedRect(QRectF(bar_x, bar_y, filled_w, bar_h), bar_h//2, bar_h//2)
                # Thumb
                tx = bar_x + filled_w - int(9*sc)
                ty = int(y + row_h/2 - 9*sc)
                p.setBrush(QColor(th.text_key))
                p.drawEllipse(QRectF(tx, ty, int(18*sc), int(18*sc)))
                # Value label
                p.setFont(fnt_val); p.setPen(QColor(th.text_hint))
                val_str = str(round(value, 2)) if isinstance(value, float) else str(value)
                p.drawText(QRectF(pad + 160*sc, y, 80*sc, row_h), Qt.AlignVCenter, val_str)

                # ± buttons
                pm_w = int(26*sc); pm_h = int(26*sc)
                dec_r = QRectF(bar_x - pm_w - 6*sc, y + (row_h-pm_h)/2, pm_w, pm_h)
                inc_r = QRectF(bar_x + bar_w + 6*sc, y + (row_h-pm_h)/2, pm_w, pm_h)
                for (label_btn, btn_rect, btn_id) in [("-", dec_r, f"dec_{idx}"), ("+", inc_r, f"inc_{idx}")]:
                    h_id = 200 + idx * 2 + (0 if label_btn=="-" else 1)
                    is_hov = (self._settings_hover == h_id)
                    p.setBrush(QColor(th.key_hover) if is_hov else QColor(th.key_action))
                    p.setPen(Qt.NoPen)
                    p.drawRoundedRect(btn_rect, 6, 6)
                    p.setPen(QColor(th.text_key)); p.setFont(fnt)
                    p.drawText(btn_rect, Qt.AlignCenter, label_btn)

            y += row_h + int(4*sc)

        # Lang rotation buttons
        lang_y = y
        p.setFont(fnt); p.setPen(QColor(th.text_key))
        p.drawText(QRectF(pad, lang_y, 160*sc, row_h), Qt.AlignVCenter | Qt.AlignLeft, "Languages")
        bx2 = pad + int(160 * sc)
        by2 = int(lang_y + (row_h - int(28*sc))/2)
        for li, lang in enumerate(self._system_langs):
            lb_w = int(46*sc); lb_h = int(28*sc)
            lb_r = QRectF(bx2, by2, lb_w, lb_h)
            is_cur = (lang == self._language)
            hov_l  = (self._settings_hover == 500 + li)
            p.setBrush(QColor(th.key_mod_on) if is_cur else (QColor(th.key_hover) if hov_l else QColor(th.key_action)))
            p.setPen(Qt.NoPen); p.drawRoundedRect(lb_r, 7, 7)
            p.setPen(QColor(th.text_key)); p.setFont(fnt_val)
            p.drawText(lb_r, Qt.AlignCenter, lang.upper())
            bx2 += lb_w + int(6*sc)

    def _paint_clipboard_panel(self, p: QPainter, W: float, H: float) -> None:
        th   = self._theme
        sc   = self.config.scale
        pad  = int(12 * sc)
        item_h = int(42 * sc)
        fnt  = QFont("Segoe UI", max(8, int(10 * sc)))
        fnt_hdr = QFont("Segoe UI", max(10, int(13 * sc)), QFont.Bold)

        p.setPen(QColor(th.text_key)); p.setFont(fnt_hdr)
        p.drawText(QRectF(pad, 0, W-pad*2, int(36*sc)), Qt.AlignVCenter | Qt.AlignLeft, "📋  Clipboard")

        back_rect = QRectF(W - pad - 60*sc, 6*sc, 56*sc, 26*sc)
        p.setBrush(QColor(th.key_action)); p.setPen(Qt.NoPen)
        p.drawRoundedRect(back_rect, 8, 8)
        p.setPen(QColor(th.text_key)); p.setFont(fnt)
        p.drawText(back_rect, Qt.AlignCenter, "← Back")

        items = self._clipboard.items()
        y = int(40 * sc)
        p.setFont(fnt)
        for idx, item in enumerate(items[:8]):
            ir = QRectF(pad, y, W - pad*2, item_h - int(4*sc))
            p.setBrush(QColor(th.key_normal)); p.setPen(Qt.NoPen)
            p.drawRoundedRect(ir, 8, 8)
            p.setPen(QColor(th.text_key))
            clipped = item[:80].replace("\n", "↵") + ("…" if len(item) > 80 else "")
            p.drawText(ir.adjusted(10, 0, -10, 0), Qt.AlignVCenter | Qt.AlignLeft, clipped)
            y += item_h
        if not items:
            p.setPen(QColor(th.text_hint))
            p.drawText(QRectF(0, int(60*sc), W, int(40*sc)), Qt.AlignCenter, "Clipboard is empty")

    def _paint_statusbar(self, p: QPainter, W: float, H: float) -> None:
        th = self._theme; sc = self.config.scale
        info_parts = [self._language.upper()]
        if self._shift_state == "lock": info_parts.append("CAPS")
        elif self._shift_state == "once": info_parts.append("⇧")
        if self._ctrl_on: info_parts.append("Ctrl")
        if self._alt_on:  info_parts.append("Alt")
        if self._scanning_enabled: info_parts.append("SCAN")
        if self._swipe_enabled: info_parts.append("~Swipe")

        text = "  ".join(info_parts)
        p.setPen(QColor(th.text_hint))
        p.setFont(QFont("Segoe UI", max(7, int(9*sc))))
        p.drawText(QRectF(8, H-18*sc, W-16, 16*sc), Qt.AlignVCenter | Qt.AlignRight, text)


# ═══════════════════════════════════════════════════════════════════════════
#  KEYBOARD MANAGER  (overlay compatibility wrapper)
# ═══════════════════════════════════════════════════════════════════════════
class KeyboardManager(QObject):
    """Thin compatibility wrapper — drop-in for dwell_click_system.py."""

    def __init__(self):
        super().__init__()
        self._kb = ModernOnScreenKeyboard()

    def show_near(self, x: int, y: int): self._kb.show_near(x, y)
    def hide(self):                       self._kb.hide()
    def contains(self, x: int, y: int) -> bool:
        # Always False — keyboard is not a safe zone for the overlay.
        return False
    def geometry_contains(self, x: int, y: int) -> bool:
        return self._kb.geometry_contains(x, y)
    def update_cursor(self, x: int, y: int): self._kb.update_cursor(x, y)
    def isVisible(self) -> bool:          return self._kb.isVisible()
    def uses_external_dwell(self) -> bool: return False
    def keyboard(self) -> ModernOnScreenKeyboard: return self._kb


# ═══════════════════════════════════════════════════════════════════════════
#  QUICK DEMO
# ═══════════════════════════════════════════════════════════════════════════
def _demo():
    import sys
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    cfg = KeyboardConfig()
    cfg.autocomplete_enabled = True
    cfg.suggestion_count     = 3

    kb = ModernOnScreenKeyboard(config=cfg)
    kb.show_near(300, 600)

    import signal
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    pulse = QTimer(); pulse.start(250); pulse.timeout.connect(lambda: None)
    sys.exit(app.exec())


if __name__ == "__main__":
    _demo()