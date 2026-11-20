"""overlay.py — full-virtual-desktop transparent overlay window.

Design
------
* Covers the entire virtual desktop (all monitors combined) so the
  sprite can walk across screen boundaries without clipping.
* Frameless, transparent background, always on top.
* Hidden from the taskbar and Alt-Tab (WS_EX_TOOLWINDOW + Qt.Tool).
* Mouse clicks pass through:
    Qt side  — Qt.WindowTransparentForInput
    Win32    — WS_EX_TRANSPARENT | WS_EX_LAYERED applied in showEvent
               after the HWND exists, so Qt cannot reset them.
* 60 fps QTimer drives self.update(); paintEvent delegates to a
  registered draw callback so this module knows nothing about game objects.
* hwnd() exposes the native handle so WindowWatcher can exclude this
  window from its snapshot.
"""

from __future__ import annotations

from typing import Callable

import win32con
import win32gui
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

DrawCallback = Callable[[QPainter], None]


# ---------------------------------------------------------------------------
# Virtual-desktop geometry
# ---------------------------------------------------------------------------

def _virtual_desktop_rect() -> tuple[int, int, int, int]:
    """(x, y, w, h) bounding rect of all monitors in logical pixels."""
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication must exist before querying screens.")
    primary = app.primaryScreen()
    if primary is None:
        raise RuntimeError("No primary screen found.")
    vg = primary.virtualGeometry()
    return vg.x(), vg.y(), vg.width(), vg.height()


# ---------------------------------------------------------------------------
# Win32 click-through styles
# ---------------------------------------------------------------------------

def _apply_clickthrough_styles(hwnd: int) -> None:
    ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    ex |= (
        win32con.WS_EX_LAYERED
        | win32con.WS_EX_TRANSPARENT
        | win32con.WS_EX_TOOLWINDOW
        | win32con.WS_EX_NOACTIVATE
    )
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)


# ---------------------------------------------------------------------------
# Default draw callback — red square for visual confirmation
# ---------------------------------------------------------------------------

def _draw_debug_square(painter: QPainter) -> None:
    rect = painter.device().rect()  # type: ignore[union-attr]
    cx, cy, side = rect.width() // 2, rect.height() // 2, 60
    painter.setBrush(QColor("red"))
    painter.setPen(QPen(QColor("darkred"), 2))
    painter.drawRect(cx - side // 2, cy - side // 2, side, side)


# ---------------------------------------------------------------------------
# Overlay widget
# ---------------------------------------------------------------------------

class Overlay(QWidget):
    """Transparent, click-through, full-virtual-desktop overlay."""

    _REPAINT_HZ: int = 60

    def __init__(self) -> None:
        super().__init__(parent=None)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

        self._draw_callback: DrawCallback = _draw_debug_square
        self._fit_to_virtual_desktop()

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(1000 // self._REPAINT_HZ)
        self._timer.timeout.connect(self.update)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_draw_callback(self, callback: DrawCallback) -> None:
        """Replace the draw function.  Takes effect on the next frame."""
        self._draw_callback = callback

    def hwnd(self) -> int:
        """Return the native Win32 window handle (0 before first show)."""
        # winId() returns a sip.voidptr; int() converts it safely.
        return int(self.winId()) if self.isVisible() else 0

    def start(self) -> None:
        """Show the overlay and start the repaint loop."""
        self._fit_to_virtual_desktop()
        self.show()
        self._timer.start()

    def stop(self) -> None:
        """Stop repainting and hide."""
        self._timer.stop()
        self.hide()

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        _apply_clickthrough_styles(int(self.winId()))

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        try:
            # CompositionMode_Source lets us write alpha=0 (fully transparent).
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Source
            )
            painter.fillRect(self.rect(), QColor(0, 0, 0, 0))

            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )
            self._draw_callback(painter)
        finally:
            painter.end()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fit_to_virtual_desktop(self) -> None:
        x, y, w, h = _virtual_desktop_rect()
        self.setGeometry(x, y, w, h)
