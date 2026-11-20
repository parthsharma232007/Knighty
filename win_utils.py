"""win_utils.py — Windows-specific helpers.

Responsibilities
----------------
- Enable Per-Monitor V2 DPI awareness (must precede QApplication).
- Return usable geometry for one screen or all screens combined.
- Detect whether a fullscreen application is covering the monitor the
  knight is standing on.
- Detect whether the Windows session is currently locked.
- Thin wrappers around GetForegroundWindow and related Win32 calls so
  the rest of the codebase never imports win32api/ctypes directly.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging

from PySide6.QtCore import QRect
from PySide6.QtGui import QScreen
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 handles and constants
# ---------------------------------------------------------------------------

_user32   = ctypes.windll.user32    # type: ignore[attr-defined]
_kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

# SetProcessDpiAwarenessContext value for Per-Monitor V2 (Windows 10 1703+).
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

# GetMonitorInfo / MONITORINFO flags.
_MONITOR_DEFAULTTONEAREST = 0x00000002
_MONITORINFOF_PRIMARY     = 0x00000001

# WTSQuerySessionInformation class for the connection state.
_WTSConnectState     = 0
_WTSActive           = 0   # session is active (not locked)
_WTSDisconnected     = 4   # disconnected (screen locked / RDP disconnected)

# Access flag for OpenInputDesktop.
_DF_ALLOWOTHERACCOUNTHOOK = 0x0001


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    ctypes.c_ulong),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork",    ctypes.wintypes.RECT),
        ("dwFlags",   ctypes.c_ulong),
    ]


# ---------------------------------------------------------------------------
# DPI awareness
# ---------------------------------------------------------------------------

def enable_dpi_awareness() -> None:
    """Set Per-Monitor V2 DPI awareness.  Call before QApplication()."""
    try:
        _user32.SetProcessDpiAwarenessContext(
            _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass   # Windows 7 — continue without DPI awareness.


# ---------------------------------------------------------------------------
# Screen helpers
# ---------------------------------------------------------------------------

def _require_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication must exist before querying screens.")
    return app  # type: ignore[return-value]


def primary_screen() -> QScreen:
    screen = _require_app().primaryScreen()
    if screen is None:
        raise RuntimeError("No primary screen found.")
    return screen


def screen_for_pet() -> QScreen:
    """Return the screen the pet lives on (primary)."""
    return primary_screen()


def usable_rect(screen: QScreen | None = None) -> QRect:
    """Available geometry of *screen* in logical pixels (excludes taskbar)."""
    if screen is None:
        screen = screen_for_pet()
    return screen.availableGeometry()


def screen_dpi_scale(screen: QScreen | None = None) -> float:
    """Logical→physical pixel ratio for *screen*."""
    if screen is None:
        screen = screen_for_pet()
    return screen.devicePixelRatio()


def virtual_desktop_rect() -> tuple[int, int, int, int]:
    """(x, y, w, h) bounding rect of all monitors in logical pixels."""
    vg = primary_screen().virtualGeometry()
    return vg.x(), vg.y(), vg.width(), vg.height()


def all_usable_rects() -> list[QRect]:
    """Return availableGeometry() for every attached screen.

    Used by pet.py when MULTI_MONITOR is True.  The list is ordered with
    the primary screen first; secondary screens follow in Qt's enumeration
    order.  All values are in logical pixels.
    """
    app = _require_app()
    screens: list[QScreen] = app.screens()
    primary = app.primaryScreen()

    # Put primary first so index 0 is always the main screen.
    ordered: list[QScreen] = []
    if primary is not None:
        ordered.append(primary)
    for s in screens:
        if s is not primary:
            ordered.append(s)

    return [s.availableGeometry() for s in ordered]


def screen_at_x(x: float) -> QRect:
    """Return the usable rect of whichever screen contains logical x coord.

    Falls back to the primary screen if no screen claims the coordinate.
    """
    app = _require_app()
    for screen in app.screens():
        rect = screen.availableGeometry()
        if rect.left() <= x <= rect.right():
            return rect
    return usable_rect()


# ---------------------------------------------------------------------------
# Fullscreen detection
# ---------------------------------------------------------------------------

def is_fullscreen_app_running(pet_x: float) -> bool:
    """Return True when a fullscreen application covers the monitor at pet_x.

    Strategy
    --------
    1.  Get the foreground window HWND.
    2.  Skip shell / desktop windows (Progman, WorkerW) — they are always
        full-screen but are not "apps".
    3.  Get the monitor the foreground window is on with MonitorFromWindow.
    4.  Compare the window rect (GetWindowRect) with the *full* monitor rect
        (rcMonitor from GetMonitorInfo, which includes the taskbar area).
        A true fullscreen app covers rcMonitor exactly.
    5.  Also check that the monitor overlaps the pet's current x position so
        we only hide when the fullscreen app is on the same screen as the pet.

    Returns False on any Win32 error so the pet stays visible by default.
    """
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False

        # Skip shell windows that look fullscreen but aren't apps.
        cls_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls_buf, 256)
        cls = cls_buf.value
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False

        # Get the monitor the foreground window is on.
        hmon = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return False

        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False

        # Does the foreground window cover the full monitor rect?
        wr = ctypes.wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            return False

        mon = mi.rcMonitor
        window_is_fullscreen = (
            wr.left  <= mon.left  and
            wr.top   <= mon.top   and
            wr.right >= mon.right and
            wr.bottom >= mon.bottom
        )
        if not window_is_fullscreen:
            return False

        # Is this monitor the one the pet is standing on?
        # Compare the monitor's logical rect with pet_x.
        # rcMonitor is in physical pixels; we need logical pixels.
        # Use Qt to find the screen at pet_x and compare its geometry
        # against the monitor handle.
        pet_screen_rect = screen_at_x(pet_x)
        # Map monitor physical rect to logical by checking if any Qt screen's
        # geometry overlaps the monitor's physical centre.
        mon_cx_phys = (mon.left + mon.right) // 2
        mon_cy_phys = (mon.top  + mon.bottom) // 2
        # Find the Qt screen whose physical centre matches this monitor.
        app = _require_app()
        for screen in app.screens():
            ratio = screen.devicePixelRatio()
            geom  = screen.geometry()
            # Convert logical geometry to physical for comparison.
            phys_left   = round(geom.left()   * ratio)
            phys_top    = round(geom.top()    * ratio)
            phys_right  = round(geom.right()  * ratio)
            phys_bottom = round(geom.bottom() * ratio)
            if (phys_left <= mon_cx_phys <= phys_right and
                    phys_top <= mon_cy_phys <= phys_bottom):
                # This Qt screen corresponds to the fullscreen monitor.
                avail = screen.availableGeometry()
                # Does the pet's x fall within this screen?
                return avail.left() <= pet_x <= avail.right()

        return False

    except (OSError, AttributeError):
        log.debug("is_fullscreen_app_running: Win32 error (ignored).")
        return False


# ---------------------------------------------------------------------------
# Session-lock detection
# ---------------------------------------------------------------------------

def is_session_locked() -> bool:
    """Return True when the Windows session is locked.

    Uses OpenInputDesktop: when the screen is locked, the interactive
    desktop is not accessible and OpenInputDesktop returns NULL.
    This is the most reliable lock-detection method that does not require
    WTS APIs (which need a service context on some Windows editions).

    Returns False on error so the pet stays running by default.
    """
    try:
        # OpenInputDesktop(0, FALSE, DESKTOP_SWITCHDESKTOP)
        # DESKTOP_SWITCHDESKTOP = 0x0100
        hdesk = _user32.OpenInputDesktop(0, False, 0x0100)
        if hdesk:
            _user32.CloseDesktop(hdesk)
            return False
        # NULL handle → interactive desktop not available → locked.
        return True
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Foreground window
# ---------------------------------------------------------------------------

def get_foreground_hwnd() -> int:
    """Return the HWND of the current foreground window (0 if none)."""
    try:
        hwnd = _user32.GetForegroundWindow()
        return int(hwnd) if hwnd else 0
    except (AttributeError, OSError):
        return 0
