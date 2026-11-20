"""idle_monitor.py — idle / active state detection via Win32.

Uses GetLastInputInfo (user32.dll) to measure time since the last
keyboard or mouse event at the OS level.  No pynput, no background
threads — the QTimer fires on the main thread and the Win32 call is
instantaneous.

Signals
-------
went_idle    — emitted once when idleness crosses IDLE_TIMEOUT_SECONDS.
came_active  — emitted once when input resumes after a went_idle.

Properties
----------
is_idle          — bool, synchronous query.
seconds_inactive — float, raw elapsed seconds from Win32.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging

from PySide6.QtCore import QObject, QTimer, Signal

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 binding
# ---------------------------------------------------------------------------

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),   # tick count of last input event
    ]


def _seconds_since_last_input() -> float:
    """Return seconds elapsed since the last keyboard/mouse event."""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))  # type: ignore[attr-defined]
    # GetTickCount() wraps at ~49.7 days; subtraction in uint32 handles wrap.
    tick_now   = ctypes.windll.kernel32.GetTickCount()          # type: ignore[attr-defined]
    elapsed_ms = ctypes.c_uint(tick_now - info.dwTime).value
    return elapsed_ms / 1000.0


# ---------------------------------------------------------------------------
# IdleMonitor
# ---------------------------------------------------------------------------

class IdleMonitor(QObject):
    """Polls Win32 idle time and emits went_idle / came_active."""

    went_idle:   Signal = Signal()
    came_active: Signal = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._idle = False

        self._timer = QTimer(self)
        self._timer.setInterval(config.IDLE_POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin polling.  Safe to call multiple times."""
        if not self._timer.isActive():
            self._timer.start()
            log.debug("IdleMonitor started (poll=%d ms, threshold=%.1f s)",
                      config.IDLE_POLL_MS, config.IDLE_TIMEOUT_SECONDS)

    def stop(self) -> None:
        """Stop polling and reset state."""
        self._timer.stop()
        self._idle = False
        log.debug("IdleMonitor stopped.")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_idle(self) -> bool:
        return self._idle

    @property
    def seconds_inactive(self) -> float:
        """Raw seconds since last input, queried on demand."""
        return _seconds_since_last_input()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        elapsed  = _seconds_since_last_input()
        now_idle = elapsed >= config.IDLE_TIMEOUT_SECONDS

        if now_idle and not self._idle:
            self._idle = True
            log.debug("User idle (%.1f s).", elapsed)
            self.went_idle.emit()
        elif not now_idle and self._idle:
            self._idle = False
            log.debug("User active (was idle %.1f s).", elapsed)
            self.came_active.emit()
