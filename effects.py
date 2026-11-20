"""effects.py — stateless visual effect draw helpers.

Each function receives an active QPainter and all the parameters it
needs; none of them hold state.  Callers (pet.py, overlay.py) are
responsible for deciding when to call them.

Current effects
---------------
draw_shadow    — soft elliptical shadow under the sprite's feet.

Placeholders (no-op until assets exist)
-----------------------------------------
draw_land_puff — secondary landing puff; jump frames already contain
                 the landing effect so this is intentionally empty.
draw_shake     — screen-shake offset helper for future use.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QRadialGradient


def draw_shadow(
    painter: QPainter,
    bx: float,
    by: float,
    width: float = 40.0,
    height: float = 8.0,
    opacity: float = 0.35,
) -> None:
    """Draw a soft elliptical drop-shadow centred at the feet position.

    Parameters
    ----------
    painter : active QPainter
    bx, by  : bottom-center anchor (feet position) in painter coordinates
    width   : full width of the shadow ellipse in logical pixels
    height  : full height of the shadow ellipse in logical pixels
    opacity : alpha multiplier (0.0 = invisible, 1.0 = fully opaque)
    """
    painter.save()

    cx = bx
    cy = by - height / 2.0   # centre the ellipse just below the feet

    # Radial gradient: dark centre fading to transparent edge.
    grad = QRadialGradient(QPointF(cx, cy), width / 2.0)
    alpha = int(max(0, min(255, opacity * 255)))
    grad.setColorAt(0.0, QColor(0, 0, 0, alpha))
    grad.setColorAt(1.0, QColor(0, 0, 0, 0))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(grad)
    painter.drawEllipse(QRectF(cx - width / 2.0, cy - height / 2.0, width, height))

    painter.restore()


# ---------------------------------------------------------------------------
# Placeholders — signatures are fixed so pet.py can call them already.
# ---------------------------------------------------------------------------

def draw_land_puff(
    painter: QPainter,
    bx: float,
    by: float,
    progress: float,          # 0.0 (just landed) → 1.0 (fully faded)
) -> None:
    """Placeholder: secondary landing puff.  Currently a no-op.

    The jump sprite already contains the landing effect on its last frame.
    """


def draw_shake_offset(progress: float, amplitude: float = 6.0) -> tuple[float, float]:
    """Return a (dx, dy) shake offset for screen-shake effects.

    Parameters
    ----------
    progress  : 0.0 = shake start, 1.0 = fully settled
    amplitude : maximum pixel displacement at progress=0

    Returns a (dx, dy) tuple the caller should add to draw positions.
    Currently returns (0, 0); wire up when shake is needed.
    """
    return 0.0, 0.0
