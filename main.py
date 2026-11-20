"""main.py — entry point for Knighty.

Normal startup
--------------
1.  Single-instance guard via a named Win32 mutex.
2.  Per-Monitor V2 DPI awareness (must precede QApplication).
3.  QApplication — no auto-quit on last window close.
4.  Pet → Tray → IdleMonitor → WindowWatcher.
5.  Connect quit_requested → _shutdown().
6.  Start monitors.
7.  Event loop.

Demo mode  (--demo)
-------------------
Steps 1-3 identical.  Then:
4.  Pet only — no tray, no monitors.
5.  Bounce loop via go_to(); print state each second.
6.  Event loop — Ctrl-C exits.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import logging
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

import win_utils

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single-instance mutex
# ---------------------------------------------------------------------------

_MUTEX_NAME           = "KnightyDesktopPet_SingleInstanceMutex"
_ERROR_ALREADY_EXISTS = 183
_mutex_handle: ctypes.wintypes.HANDLE | None = None


def _acquire_single_instance_lock() -> bool:
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        log.warning("CreateMutexW failed; skipping single-instance check.")
        return True
    _mutex_handle = handle
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        _mutex_handle = None
        return False
    return True


# ---------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------

def _run_demo(pet: "Pet") -> None:  # type: ignore[name-defined]
    """Bounce the pet left-right, pause 2 s at each end, print state/frame."""
    pet._wander_timer.stop()
    pet._sleep_timer.stop()

    rect   = win_utils.usable_rect()
    half_w = pet._anims["idle"].frame_w / 2.0
    left_x  = float(rect.left())  + half_w
    right_x = float(rect.right()) - half_w

    going_right: list[bool] = [True]

    def _arrive() -> None:
        target = right_x if going_right[0] else left_x
        going_right[0] = not going_right[0]
        QTimer.singleShot(2000, lambda: pet.go_to(target, on_arrive=_arrive))

    pet.go_to(right_x, on_arrive=_arrive)

    def _report() -> None:
        anim  = pet._animator.animation
        frame = pet._animator._current_index()
        print(f"  anim={anim.name:6s}  frame={frame:2d}  "
              f"x={pet._x:7.1f}  {'←' if pet._facing_left else '→'}")

    reporter = QTimer()
    reporter.setInterval(1000)
    reporter.timeout.connect(_report)
    reporter.start()
    pet._demo_reporter = reporter  # type: ignore[attr-defined]

    print(f"Demo — Ctrl-C to exit.  x range: {left_x:.0f}–{right_x:.0f} px")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="knighty", description="Knighty desktop pet")
    p.add_argument("--demo",      action="store_true",
                   help="Bounce demo with state output")
    p.add_argument("--log-level", default="WARNING",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   metavar="LEVEL")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def _build_shutdown(app: QApplication, *stoppables) -> "Callable[[], None]":  # type: ignore[name-defined]
    def _shutdown() -> None:
        for s in stoppables:
            if s is not None:
                s.stop()
        app.quit()
    return _shutdown


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )

    # 1. Single instance
    if not args.demo:
        if not _acquire_single_instance_lock():
            print("Knighty is already running.", file=sys.stderr)
            sys.exit(1)

    # 2. DPI awareness
    win_utils.enable_dpi_awareness()

    # 3. QApplication
    app = QApplication(sys.argv)
    app.setApplicationName("Knighty")
    app.setApplicationVersion("1.0.0")
    app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app.setQuitOnLastWindowClosed(False)

    # Deferred imports — Qt must exist before QPixmap / QTimer are used.
    from idle_monitor    import IdleMonitor
    from pet             import Pet
    from tray            import Tray
    from window_watcher  import WindowWatcher

    # 4. Build
    pet = Pet()

    if args.demo:
        _run_demo(pet)
        # Keep event loop responsive to Ctrl-C.
        _keepalive = QTimer()
        _keepalive.setInterval(200)
        _keepalive.timeout.connect(lambda: None)
        _keepalive.start()
        sys.exit(app.exec())

    # Normal mode
    monitor = IdleMonitor()
    pet.connect_idle_monitor(monitor)

    # Give WindowWatcher the overlay HWND so it filters out our own window.
    watcher = WindowWatcher(own_hwnd=pet.overlay().hwnd())
    watcher.window_opened.connect(lambda _hwnd, _title: pet.on_window_event(is_close=False))
    watcher.window_closed.connect(lambda _hwnd: pet.on_window_event(is_close=True))

    tray = Tray(pet)

    # 5. Quit wiring
    shutdown = _build_shutdown(app, monitor, watcher)
    tray.quit_requested.connect(shutdown)

    # 6. Start
    monitor.start()
    watcher.start()

    # 7. Event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
