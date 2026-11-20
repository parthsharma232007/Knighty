"""tray.py — system tray icon and context menu.

Menu
----
    Pause / Resume   — freezes / unfreezes all timers and animation
    Wake knight      — exits SLEEP; disabled unless pet is sleeping
    ──────────────
    Quit             — emits quit_requested; main.py shuts down cleanly

Icon: loaded from assets/knight/icon.png when present; otherwise a
knight-helmet silhouette is painted programmatically.

Left-click on the tray icon opens the same menu as right-click.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QPoint, QSize, Qt, Signal

log = logging.getLogger(__name__)
from PySide6.QtGui import (
    QAction, QColor, QIcon, QImage, QPainter, QPixmap, QPolygon,
)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

import config
from pet import Pet, State


# ---------------------------------------------------------------------------
# Icon helpers
# ---------------------------------------------------------------------------

def _make_fallback_icon(size: int = 32) -> QIcon:
    img = QImage(QSize(size, size), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size

    p.setBrush(QColor("#2a2a2a"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRect(int(s*.15), int(s*.42), int(s*.70), int(s*.14))  # visor

    p.setBrush(QColor("#7a7a8a"))
    p.setPen(QColor("#3a3a4a"))
    p.drawEllipse(int(s*.15), int(s*.08), int(s*.70), int(s*.56))  # dome

    p.setBrush(QColor("#6a6a7a"))
    p.setPen(QColor("#3a3a4a"))
    p.drawRoundedRect(int(s*.10), int(s*.44), int(s*.18), int(s*.30), 3, 3)
    p.drawRoundedRect(int(s*.72), int(s*.44), int(s*.18), int(s*.30), 3, 3)

    p.setBrush(QColor("#5a5a6a"))
    p.setPen(QColor("#3a3a4a"))
    p.drawPolygon(QPolygon([
        QPoint(int(s*.25), int(s*.72)), QPoint(int(s*.75), int(s*.72)),
        QPoint(int(s*.68), int(s*.90)), QPoint(int(s*.32), int(s*.90)),
    ]))

    p.setBrush(QColor("#cc3333"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(int(s*.40), int(s*.02), int(s*.20), int(s*.14))  # plume

    p.end()
    return QIcon(QPixmap.fromImage(img))


def _load_icon() -> QIcon:
    path = config.ASSETS_DIR / "icon.png"
    return QIcon(str(path)) if path.exists() else _make_fallback_icon()


# ---------------------------------------------------------------------------
# Tray
# ---------------------------------------------------------------------------

class Tray(QObject):
    """System tray icon with Pause/Resume, Wake knight, and Quit."""

    quit_requested: Signal = Signal()

    def __init__(self, pet: Pet, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pet = pet

        self._icon = QSystemTrayIcon(_load_icon(), parent=self)
        self._icon.setToolTip(config.TRAY_TOOLTIP)

        self._menu = QMenu()

        self._pause_action = QAction("Pause", self._menu)
        self._pause_action.triggered.connect(self._toggle_pause)
        self._menu.addAction(self._pause_action)

        self._wake_action = QAction("Wake knight", self._menu)
        self._wake_action.triggered.connect(self._pet.wake)
        self._menu.addAction(self._wake_action)

        self._test_jump_action = QAction("Test jump", self._menu)
        self._test_jump_action.triggered.connect(self._pet.test_jump)
        self._menu.addAction(self._test_jump_action)

        self._react_action = QAction("React to apps", self._menu)
        self._react_action.setCheckable(True)
        self._react_action.setChecked(config.REACT_TO_WINDOWS)
        self._react_action.triggered.connect(self._toggle_react)
        self._menu.addAction(self._react_action)

        self._menu.addSeparator()

        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(quit_action)

        self._icon.setContextMenu(self._menu)
        self._menu.aboutToShow.connect(self._sync_actions)
        self._icon.activated.connect(self._on_activated)
        self._icon.show()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _toggle_react(self) -> None:
        config.REACT_TO_WINDOWS = self._react_action.isChecked()
        log.debug("REACT_TO_WINDOWS set to %s.", config.REACT_TO_WINDOWS)

    def _toggle_pause(self) -> None:
        if self._pet.paused:
            self._pet.resume()
            self._pause_action.setText("Pause")
        else:
            self._pet.pause()
            self._pause_action.setText("Resume")

    def _sync_actions(self) -> None:
        """Refresh labels/enabled state just before the menu opens."""
        self._wake_action.setEnabled(self._pet._state == State.SLEEP)
        self._test_jump_action.setEnabled(
            not self._pet.paused and self._pet._state != State.JUMP
        )
        self._react_action.setChecked(config.REACT_TO_WINDOWS)
        self._pause_action.setText("Resume" if self._pet.paused else "Pause")

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            geom = self._icon.geometry()
            self._menu.popup(QPoint(geom.left(), geom.top()))
