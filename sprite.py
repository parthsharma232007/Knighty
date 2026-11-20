"""sprite.py — animation loading and playback.

Two public classes
------------------
Animation
    Loads every frame PNG for one named animation from disk into QPixmaps
    (once, at construction time).  Holds metadata: fps, loop mode, frame
    size.  Stateless — multiple Animators can share one Animation object.

Animator
    Plays an Animation using wall-clock elapsed time, not frame counters.
    Supports:
      • loop / pingpong / once loop modes
      • play-once with an optional finished callback
      • holding the last frame after a once-animation ends
      • playing in reverse (reverse flag flips playback direction)
      • returning the current frame optionally mirrored horizontally
      • returning the current frame rotated by an arbitrary angle

One public helper
-----------------
draw_bottom_center(painter, pixmap, bx, by, angle, mirror)
    Draws *pixmap* so its bottom-center is at (bx, by).  Rotation and
    mirroring are applied around that anchor, so the feet stay on the
    same baseline regardless of frame size.

If a frame PNG is missing, a magenta placeholder of the declared frame
size is used and a warning is logged.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPixmap, QTransform

import config

log = logging.getLogger(__name__)

# Type alias for the finished callback: () -> None
FinishedCallback = Callable[[], None]

# Loop mode literals (kept as plain strings to match animations.json).
_LOOP      = "loop"
_PINGPONG  = "pingpong"
_ONCE      = "once"


# ---------------------------------------------------------------------------
# Placeholder frame
# ---------------------------------------------------------------------------

def _make_placeholder(w: int, h: int) -> QPixmap:
    """Return a magenta rectangle of size w×h as a missing-frame stand-in."""
    px = QPixmap(w, h)
    px.fill(QColor("magenta"))
    return px


# ---------------------------------------------------------------------------
# Animation — immutable after construction
# ---------------------------------------------------------------------------

class Animation:
    """Loaded frames + metadata for one named animation.

    Parameters
    ----------
    name:
        Key from animations.json (e.g. "idle", "run").
    scale:
        Uniform scale applied to every frame at load time.
        Defaults to config.DISPLAY_SCALE.
    """

    def __init__(self, name: str, scale: float | None = None) -> None:
        if scale is None:
            scale = config.DISPLAY_SCALE

        raw = config.ANIMATIONS.get(name)
        if raw is None:
            raise KeyError(f"Animation '{name}' not found in animations.json")

        self.name: str       = name
        self.fps: float      = float(raw["fps"])
        self.loop: str       = raw["loop"]          # "loop" | "pingpong" | "once"
        self.frame_count: int = int(raw["frames"])

        # Declared (unscaled) frame size from the JSON.
        fw, fh = raw["frame_size"]
        self._native_w: int = fw
        self._native_h: int = fh
        self._scale: float  = scale

        self.frames: list[QPixmap] = self._load_frames(scale)

    # -- frame size in display pixels (after scaling) --

    @property
    def frame_w(self) -> int:
        return round(self._native_w * self._scale)

    @property
    def frame_h(self) -> int:
        return round(self._native_h * self._scale)

    # -- frame duration in seconds --

    @property
    def spf(self) -> float:
        """Seconds per frame."""
        return 1.0 / self.fps

    # -- loading --

    def _load_frames(self, scale: float) -> list[QPixmap]:
        folder = config.FRAMES_DIR / self.name
        frames: list[QPixmap] = []
        fw = round(self._native_w * scale)
        fh = round(self._native_h * scale)

        for i in range(self.frame_count):
            path = folder / f"{self.name}_{i:02d}.png"
            if not path.exists():
                log.warning(
                    "Frame missing: %s — using magenta placeholder (%dx%d)",
                    path, fw, fh,
                )
                frames.append(_make_placeholder(fw, fh))
                continue

            px = QPixmap(str(path))
            if px.isNull():
                log.warning(
                    "Failed to load pixmap: %s — using magenta placeholder (%dx%d)",
                    path, fw, fh,
                )
                frames.append(_make_placeholder(fw, fh))
                continue

            if scale != 1.0:
                px = px.scaled(
                    fw, fh,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            frames.append(px)

        return frames


# ---------------------------------------------------------------------------
# Animator — stateful playback engine
# ---------------------------------------------------------------------------

class Animator:
    """Drives an Animation using elapsed wall-clock time.

    Usage
    -----
        anim = Animation("idle")
        player = Animator(anim)

        # inside a paint callback:
        pixmap = player.current_pixmap(mirror=False)

    Switching animations
    --------------------
        player.set_animation(Animation("run"))
        # The internal clock keeps running; there is no hidden timer reset.
        # Time is re-based to t=0 of the new animation at the moment of the
        # switch so the first frame of the new clip shows immediately.

    Reverse playback
    ----------------
        player.reverse = True   # plays frames last→first
        player.reverse = False  # normal

    Play-once with callback
    -----------------------
        player.set_animation(sit_anim, on_finished=lambda: go_idle())
        # The callback fires once when the last frame is reached.
        # The last frame is held indefinitely until set_animation() is called.
    """

    def __init__(self, animation: Animation) -> None:
        self._anim: Animation                    = animation
        self._start_time: float                  = time.monotonic()
        self._reverse: bool                      = False
        self._finished: bool                     = False
        self._on_finished: FinishedCallback | None = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @property
    def reverse(self) -> bool:
        """True → frames play last→first."""
        return self._reverse

    @reverse.setter
    def reverse(self, value: bool) -> None:
        if value == self._reverse:
            return
        self._reverse = value
        # Re-base time so the transition feels seamless: map the current
        # logical position in the sequence to an equivalent t in the new
        # direction.  For simplicity, restart from the current frame.
        self._start_time = time.monotonic() - self._elapsed_for_index(
            self._current_index()
        )

    def set_animation(
        self,
        animation: Animation,
        *,
        on_finished: FinishedCallback | None = None,
    ) -> None:
        """Switch to *animation* immediately, starting from its first frame.

        *on_finished* is called once when a 'once' animation reaches its
        last frame.  Ignored for loop/pingpong animations.
        """
        self._anim        = animation
        self._start_time  = time.monotonic()
        self._reverse     = False
        self._finished    = False
        self._on_finished = on_finished

    # ------------------------------------------------------------------
    # Playback state
    # ------------------------------------------------------------------

    @property
    def finished(self) -> bool:
        """True after a 'once' animation has played through completely."""
        return self._finished

    @property
    def animation(self) -> Animation:
        return self._anim

    # ------------------------------------------------------------------
    # Frame queries
    # ------------------------------------------------------------------

    def current_pixmap(
        self,
        *,
        mirror: bool = False,
        angle: float = 0.0,
    ) -> QPixmap:
        """Return the pixmap for the current frame.

        Parameters
        ----------
        mirror:
            If True, flip the image horizontally (facing-left sprite).
        angle:
            Clockwise rotation in degrees.  0 = no rotation.
        """
        self._tick()
        px = self._anim.frames[self._current_index()]

        if mirror or angle:
            t = QTransform()
            if mirror:
                # Scale by -1 on X around the centre.
                t = (
                    QTransform()
                    .translate(px.width() / 2, px.height() / 2)
                    .scale(-1, 1)
                    .translate(-px.width() / 2, -px.height() / 2)
                )
            if angle:
                t2 = (
                    QTransform()
                    .translate(px.width() / 2, px.height() / 2)
                    .rotate(angle)
                    .translate(-px.width() / 2, -px.height() / 2)
                )
                t = t * t2
            px = px.transformed(t, Qt.TransformationMode.SmoothTransformation)

        return px

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _elapsed(self) -> float:
        """Seconds since the current animation started."""
        return time.monotonic() - self._start_time

    def _current_index(self) -> int:
        """Return the frame index for the current time without side effects."""
        anim = self._anim
        n    = anim.frame_count

        if anim.loop == _ONCE:
            if self._finished:
                # Hold last frame (or first if reversed).
                return 0 if self._reverse else n - 1
            raw = int(self._elapsed() / anim.spf)
            idx = min(raw, n - 1)
            return (n - 1 - idx) if self._reverse else idx

        elif anim.loop == _LOOP:
            idx = int(self._elapsed() / anim.spf) % n
            return (n - 1 - idx) if self._reverse else idx

        else:  # pingpong
            # Full cycle = n-1 steps forward + n-1 steps back = 2*(n-1) steps.
            cycle = max(1, 2 * (n - 1))
            pos   = int(self._elapsed() / anim.spf) % cycle
            if self._reverse:
                pos = cycle - 1 - pos
            if pos < n:
                return pos
            return 2 * (n - 1) - pos   # going back

    def _elapsed_for_index(self, idx: int) -> float:
        """Inverse of _current_index: seconds that correspond to *idx*."""
        return idx * self._anim.spf

    def _tick(self) -> None:
        """Check whether a once-animation has finished and fire the callback."""
        if self._finished:
            return
        if self._anim.loop != _ONCE:
            return
        raw = int(self._elapsed() / self._anim.spf)
        if raw >= self._anim.frame_count - 1:
            self._finished = True
            if self._on_finished is not None:
                cb = self._on_finished
                self._on_finished = None   # prevent double-fire
                cb()


# ---------------------------------------------------------------------------
# draw_bottom_center — rendering helper
# ---------------------------------------------------------------------------

def draw_bottom_center(
    painter: QPainter,
    pixmap: QPixmap,
    bx: float,
    by: float,
    angle: float = 0.0,
    mirror: bool = False,
) -> None:
    """Draw *pixmap* so its bottom-center pixel is at (bx, by).

    Parameters
    ----------
    painter:
        Active QPainter (must already be begun).
    pixmap:
        The frame to draw (after any mirror/rotation the caller applied,
        or pass the raw frame and set mirror/angle here).
    bx, by:
        Bottom-center anchor in painter coordinates.
    angle:
        Clockwise rotation in degrees around the bottom-center anchor.
    mirror:
        If True, flip horizontally around the anchor before rotating.

    Notes
    -----
    The transform sequence is:
      1. Translate so the bottom-center of the pixmap lands on (bx, by).
      2. Mirror around that point (if requested).
      3. Rotate around that point (if non-zero).
    This ensures feet stay planted on the same baseline even when frame
    sizes differ across animations.
    """
    w = pixmap.width()
    h = pixmap.height()

    # Top-left corner when no transform is applied.
    draw_x = bx - w / 2.0
    draw_y = by - h

    need_transform = mirror or (angle != 0.0)
    if need_transform:
        painter.save()
        # Move origin to the anchor point.
        painter.translate(QPointF(bx, by))
        if mirror:
            painter.scale(-1.0, 1.0)
        if angle:
            painter.rotate(angle)
        # Draw with top-left offset relative to the new origin.
        painter.drawPixmap(
            QRectF(-w / 2.0, -h, w, h),
            pixmap,
            QRectF(0, 0, w, h),
        )
        painter.restore()
    else:
        painter.drawPixmap(QRectF(draw_x, draw_y, w, h), pixmap, QRectF(0, 0, w, h))


# ---------------------------------------------------------------------------
# Convenience loader — builds all animations defined in animations.json
# ---------------------------------------------------------------------------

def load_all_animations(scale: float | None = None) -> dict[str, Animation]:
    """Return a name→Animation dict for every entry in animations.json."""
    return {name: Animation(name, scale) for name in config.ANIMATIONS}
