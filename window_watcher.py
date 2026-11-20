"""window_watcher.py — detect real application window open/close events.

Design
------
A QTimer fires every WINDOW_POLL_MS ms on the main thread and calls
EnumWindows to snapshot all top-level windows.  No background threads.

A window counts as a "real app window" only when ALL of these hold:

    1.  IsWindowVisible(hwnd) is True.
    2.  Title is non-empty.
    3.  No owner window  (GetWindow(hwnd, GW_OWNER) == 0).
    4.  WS_EX_TOOLWINDOW is NOT set.
    5.  Not the pet's own overlay HWND.
    6.  Not DWM-cloaked  (DWMWA_CLOAKED == 0)  — a cloaked window is
        hidden by virtual-desktop switching but still alive; it stays in
        the set so we don't fire spurious close events.
    7.  Process name is not in WINDOW_PROCESS_BLOCKLIST.
    8.  Window class name is not in _SHELL_CLASS_BLOCKLIST.

False-positive suppression
--------------------------
*  Minimized windows pass rule 1 (IsWindowVisible is still True for
   minimized windows on Windows) so they stay in the set.
*  Close confirmation: a window that vanishes from one poll goes into a
   pending-close buffer.  It is only emitted as "closed" after
   WINDOW_CLOSE_CONFIRM_POLLS consecutive misses, OR immediately if
   IsWindow() returns False (handle is dead).
*  Warmup: no events are emitted during the first WINDOW_WARMUP_SECONDS
   after start().
*  Burst debounce: once an opened or closed event fires, further events
   of the same kind are suppressed for WINDOW_DEBOUNCE_SECONDS.  One
   app launch (splash + main window) produces one event.

Signals
-------
window_opened(hwnd: int, title: str)  — a new real window appeared.
window_closed(hwnd: int)              — a previously tracked window is gone.

Both are printed to stdout as well (temporary, for diagnostics).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 constants and bindings
# ---------------------------------------------------------------------------

_user32   = ctypes.windll.user32    # type: ignore[attr-defined]
_kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
_dwmapi   = ctypes.windll.dwmapi    # type: ignore[attr-defined]

_GWL_EXSTYLE          = -20
_GW_OWNER             = 4
_WS_EX_TOOLWINDOW     = 0x00000080
_DWMWA_CLOAKED        = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_WNDENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool,
    ctypes.wintypes.HWND,
    ctypes.wintypes.LPARAM,
)

# Window class names belonging to the Windows shell that should never
# count as real app windows, regardless of their title.
_SHELL_CLASS_BLOCKLIST: frozenset[str] = frozenset({
    "Shell_TrayWnd",          # taskbar
    "Shell_SecondaryTrayWnd", # taskbar on secondary monitor
    "Progman",                # Program Manager / desktop
    "WorkerW",                # desktop worker window
    "DV2ControlHost",         # start menu legacy
    "MsgrIMEWindowClass",
    "SysShadow",
    "tooltips_class32",
    "ForegroundStaging",
    "Windows.UI.Core.CoreWindow",   # UWP shell chrome
    "ApplicationFrameWindow",       # UWP app frame
    "SHELLDLL_DefView",
    "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
    "XamlExplorerHostIslandWindow",
    "SearchApp",
    "NativeHWNDHost",
})


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

def _get_title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _is_cloaked(hwnd: int) -> bool:
    """Return True when DWM is hiding this window (virtual desktop switch etc.)."""
    cloaked = ctypes.c_int(0)
    try:
        _dwmapi.DwmGetWindowAttribute(
            hwnd,
            _DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except OSError:
        return False
    return cloaked.value != 0


def _get_process_name(hwnd: int) -> str:
    """Return the lowercase exe filename for the process owning *hwnd*.

    Returns "" if the process cannot be queried (e.g. elevated process).
    """
    pid = ctypes.wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not handle:
        return ""
    try:
        buf  = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(1024)
        ok   = _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok:
            return ""
        return Path(buf.value).name.lower()
    finally:
        _kernel32.CloseHandle(handle)


def _passes_filter(hwnd: int, own_hwnd: int) -> bool:
    """Return True when *hwnd* qualifies as a real application window."""
    # Rule 5 — not the pet overlay.
    if hwnd == own_hwnd:
        return False

    # Rule 1 — visible (minimized windows still return True here).
    if not _user32.IsWindowVisible(hwnd):
        return False

    # Rule 2 — non-empty title.
    if not _get_title(hwnd):
        return False

    # Rule 3 — no owner (owned windows are sub-windows / popups).
    if _user32.GetWindow(hwnd, _GW_OWNER):
        return False

    # Rule 4 — not a tool window.
    ex_style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    if ex_style & _WS_EX_TOOLWINDOW:
        return False

    # Rule 8 — class name not on shell blocklist.
    if _get_class(hwnd) in _SHELL_CLASS_BLOCKLIST:
        return False

    # Rule 6 — not DWM-cloaked (virtual-desktop hidden windows stay alive).
    if _is_cloaked(hwnd):
        return False

    # Rule 7 — process not on blocklist.
    proc = _get_process_name(hwnd)
    if proc and proc in config.WINDOW_PROCESS_BLOCKLIST:
        return False

    return True


def _enum_windows(own_hwnd: int) -> dict[int, str]:
    """Return {hwnd: title} for all windows that pass the filter."""
    found: dict[int, str] = {}

    def _cb(hwnd: int, _lparam: int) -> bool:
        if _passes_filter(hwnd, own_hwnd):
            found[hwnd] = _get_title(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return found


# ---------------------------------------------------------------------------
# WindowWatcher
# ---------------------------------------------------------------------------

class WindowWatcher(QObject):
    """Polls EnumWindows and emits signals when real app windows appear/vanish.

    Parameters
    ----------
    own_hwnd:
        The HWND of the pet overlay, excluded from every snapshot.
        Can be updated later with set_own_hwnd().
    """

    # Emitted once per new real window.  hwnd, title.
    window_opened: Signal = Signal(int, str)

    # Emitted once a window has been confirmed gone for
    # WINDOW_CLOSE_CONFIRM_POLLS consecutive polls.
    window_closed: Signal = Signal(int)

    def __init__(self, own_hwnd: int = 0, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._own_hwnd: int = own_hwnd

        # hwnd → title for every window currently in the tracked set.
        self._live: dict[int, str] = {}

        # hwnd → number of consecutive polls it failed the filter.
        # Cleared when it passes again or when the close event fires.
        self._pending_close: dict[int, int] = {}

        # Warmup: start() records this; _poll() ignores events until
        # monotonic() >= _emit_after.
        self._emit_after: float = 0.0

        # Debounce: timestamps of the last emitted open and close events.
        self._last_opened_emit: float = -config.WINDOW_DEBOUNCE_SECONDS
        self._last_closed_emit: float = -config.WINDOW_DEBOUNCE_SECONDS

        self._timer = QTimer(self)
        self._timer.setInterval(config.WINDOW_POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_own_hwnd(self, hwnd: int) -> None:
        """Update the overlay HWND to exclude (call after overlay.show())."""
        self._own_hwnd = hwnd

    def start(self) -> None:
        """Take a silent initial snapshot then begin polling."""
        self._live = _enum_windows(self._own_hwnd)
        self._pending_close.clear()
        self._emit_after = time.monotonic() + config.WINDOW_WARMUP_SECONDS
        if not self._timer.isActive():
            self._timer.start()
        log.debug(
            "WindowWatcher started: %d windows in initial snapshot, "
            "emitting after %.1f s warmup.",
            len(self._live), config.WINDOW_WARMUP_SECONDS,
        )

    def stop(self) -> None:
        self._timer.stop()
        log.debug("WindowWatcher stopped.")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        now     = time.monotonic()
        warming = now < self._emit_after
        current = _enum_windows(self._own_hwnd)

        # ---- opened: hwnd in current but not in live ------------------
        for hwnd, title in current.items():
            if hwnd not in self._live:
                # Remove from pending-close if it briefly disappeared.
                self._pending_close.pop(hwnd, None)
                if not warming:
                    self._emit_opened(hwnd, title, now)
            # Whether new or already tracked, update title in live set.
            self._live[hwnd] = title

        # ---- missing: hwnd in live but not in current -----------------
        gone: list[int] = []
        for hwnd in list(self._live):
            if hwnd in current:
                # Still present — clear any pending-close miss count.
                self._pending_close.pop(hwnd, None)
                continue

            # Dead handle — close immediately, no confirmation needed.
            if not _user32.IsWindow(hwnd):
                gone.append(hwnd)
                if not warming:
                    self._emit_closed(hwnd, now)
                continue

            # Increment miss counter.
            misses = self._pending_close.get(hwnd, 0) + 1
            if misses >= config.WINDOW_CLOSE_CONFIRM_POLLS:
                gone.append(hwnd)
                if not warming:
                    self._emit_closed(hwnd, now)
            else:
                self._pending_close[hwnd] = misses

        for hwnd in gone:
            self._live.pop(hwnd, None)
            self._pending_close.pop(hwnd, None)

    # ------------------------------------------------------------------
    # Emit helpers (apply debounce, print, then signal)
    # ------------------------------------------------------------------

    def _emit_opened(self, hwnd: int, title: str, now: float) -> None:
        if now - self._last_opened_emit < config.WINDOW_DEBOUNCE_SECONDS:
            log.debug("open debounced: 0x%08x '%s'", hwnd, title)
            return
        self._last_opened_emit = now
        print(f"[window_watcher] opened  0x{hwnd:08x}  '{title}'")
        log.debug("Window opened: 0x%08x '%s'", hwnd, title)
        self.window_opened.emit(hwnd, title)

    def _emit_closed(self, hwnd: int, now: float) -> None:
        if now - self._last_closed_emit < config.WINDOW_DEBOUNCE_SECONDS:
            log.debug("close debounced: 0x%08x", hwnd)
            return
        self._last_closed_emit = now
        title = self._live.get(hwnd, "")
        print(f"[window_watcher] closed  0x{hwnd:08x}  '{title}'")
        log.debug("Window closed: 0x%08x '%s'", hwnd, title)
        self.window_closed.emit(hwnd)
