"""pet.py — the knight desktop pet.

State machine
-------------
    IDLE  → plays "idle" (pingpong).  After a random pause walks to the
            opposite side of the screen.  After SLEEP_AFTER_IDLE_SECONDS
            of OS-level inactivity walks to a corner and sits.
    WALK  → moves toward a target x at run / run2 speed.  On arrival
            calls the registered arrive callback then enters IDLE.
    SLEEP → "sit" plays once and holds.  Woken when the user resumes
            input (came_active from IdleMonitor) or via wake().
            wake() plays "sit" in reverse then walks to screen centre.
    JUMP  → "jump" plays once (frames contain the full arc and landing
            effect — this code adds nothing procedural).  On finish,
            returns to whichever state was interrupted.

Priority rules
--------------
    • JUMP interrupts IDLE or WALK, then returns to that state on landing.
    • If sleeping, the pet wakes first (plays sit in reverse), then jumps
      once the stand-up animation finishes.
    • A window event that arrives while already jumping is dropped (not
      queued).  The JUMP_MIN_GAP_SECONDS cooldown prevents bursts.
    • REACT_TO_WINDOWS = False disables window-triggered jumps entirely.
    • Close events play the jump at fps × CLOSE_SPEED_MULTIPLIER.

Public API
----------
    Pet()                             — creates Overlay, loads anims
    pet.overlay()                     — the Overlay widget
    pet.connect_idle_monitor(m)       — wire IdleMonitor → sleep/wake
    pet.on_window_event(is_close)     — window open/close trigger
    pet.test_jump()                   — trigger a jump (tray use)
    pet.wake()                        — force exit from SLEEP
    pet.pause() / resume()            — freeze / unfreeze everything
    pet.paused                        — bool property
"""

from __future__ import annotations

import logging
import math
import random
import time
from enum import Enum, auto
from typing import Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QPainter

import config
import win_utils
from idle_monitor import IdleMonitor
from overlay import Overlay
from sprite import Animation, Animator, draw_bottom_center, load_all_animations

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class State(Enum):
    IDLE  = auto()
    WALK  = auto()
    SLEEP = auto()
    JUMP  = auto()


# ---------------------------------------------------------------------------
# Pet
# ---------------------------------------------------------------------------

class Pet(QObject):
    """The knight: position, facing, state machine, overlay, animator."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # -- animations ---------------------------------------------------
        self._anims: dict[str, Animation] = load_all_animations()

        # -- animator -----------------------------------------------------
        self._animator = Animator(self._anims["idle"])

        # -- overlay ------------------------------------------------------
        self._overlay = Overlay()
        self._overlay.set_draw_callback(self._draw)

        # -- position & facing --------------------------------------------
        self._x: float = 0.0
        self._y: float = 0.0
        self._facing_left: bool = False
        self._reset_to_ground_centre()

        # -- state --------------------------------------------------------
        self._state: State = State.IDLE

        # -- pause --------------------------------------------------------
        self._paused: bool = False
        self._pause_time: float = 0.0
        self._wander_remaining_ms: int = 0
        self._sleep_remaining_ms: int = 0

        # -- walk ---------------------------------------------------------
        self._walk_target_x: float = 0.0
        self._walk_speed_pps: float = config.RUN_SPEED_PPS
        self._walk_anim_name: str = "run"
        self._walk_arrive_cb: Callable[[], None] | None = None
        self._last_walk_tick: float = 0.0

        # -- jump ---------------------------------------------------------
        self._pre_jump_state: State = State.IDLE
        self._last_jump_time: float = -config.JUMP_MIN_GAP_SECONDS
        self._jump_start_time: float = 0.0
        # fps multiplier for the current jump; >1 plays faster (close events).
        self._jump_fps_scale: float = 1.0
        # Last draw timestamp used to advance the animator's time-compression.
        self._jump_last_draw_time: float = 0.0
        # Set True when a jump should fire immediately after waking from sleep.
        self._jump_pending_after_wake: bool = False
        # fps_scale stored for the pending post-wake jump.
        self._jump_pending_fps_scale: float = 1.0

        # -- timers -------------------------------------------------------
        self._wander_timer = QTimer(self)
        self._wander_timer.setSingleShot(True)
        self._wander_timer.timeout.connect(self._start_wander)

        self._sleep_timer = QTimer(self)
        self._sleep_timer.setSingleShot(True)
        self._sleep_timer.timeout.connect(self._enter_sleep)

        self._walk_timer = QTimer(self)
        self._walk_timer.timeout.connect(self._walk_tick)

        # -- start --------------------------------------------------------
        self._enter_idle()
        self._overlay.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def overlay(self) -> Overlay:
        return self._overlay

    def connect_idle_monitor(self, monitor: IdleMonitor) -> None:
        """Wire IdleMonitor signals so user inactivity drives sleep/wake."""
        monitor.went_idle.connect(self._on_user_idle)
        monitor.came_active.connect(self._on_user_active)

    def on_window_event(self, is_close: bool = False) -> None:
        """Call on window open/close.  Triggers a jump per priority rules.

        Parameters
        ----------
        is_close:
            True for a window_closed event.  When CLOSE_SPEED_MULTIPLIER > 1
            the jump animation plays at fps × CLOSE_SPEED_MULTIPLIER.

        Dropped if:
          • REACT_TO_WINDOWS is False
          • paused
          • already jumping (never queued)
          • within JUMP_MIN_GAP_SECONDS of the previous jump

        If sleeping, wakes first then jumps once standing.
        """
        if not config.REACT_TO_WINDOWS:
            return
        if self._paused:
            return
        if self._state == State.JUMP:
            return  # drop — never queue
        now = time.monotonic()
        if now - self._last_jump_time < config.JUMP_MIN_GAP_SECONDS:
            return

        fps_scale = config.CLOSE_SPEED_MULTIPLIER if is_close else 1.0

        if self._state == State.SLEEP:
            self._jump_pending_after_wake = True
            self._jump_pending_fps_scale  = fps_scale
            self._enter_wake()
            return

        self._enter_jump(fps_scale=fps_scale)

    def test_jump(self) -> None:
        """Trigger a jump unconditionally, bypassing cooldown and flags.

        Intended for the tray "Test jump" item.
        Respects paused; drops silently if already jumping.
        """
        if self._paused:
            return
        if self._state == State.JUMP:
            return
        if self._state == State.SLEEP:
            self._jump_pending_after_wake = True
            self._jump_pending_fps_scale  = 1.0
            self._enter_wake()
            return
        self._enter_jump(fps_scale=1.0)

    def go_to(
        self,
        x: float,
        y: float = 0.0,
        on_arrive: Callable[[], None] | None = None,
    ) -> None:
        """Walk to x (y is ignored; pet stays on the ground line)."""
        self._start_walk(target_x=x, on_arrive=on_arrive)

    def wake(self) -> None:
        """Wake from SLEEP.  No-op in any other state."""
        if self._state == State.SLEEP:
            self._enter_wake()

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Freeze all motion and animation.  No-op if already paused."""
        if self._paused:
            return
        self._paused = True
        self._pause_time = time.monotonic()

        self._wander_remaining_ms = self._wander_timer.remainingTime()
        self._sleep_remaining_ms  = self._sleep_timer.remainingTime()

        self._walk_timer.stop()
        self._wander_timer.stop()
        self._sleep_timer.stop()

        self._overlay.stop()
        self._overlay.show()   # keep last frame visible

    def resume(self) -> None:
        """Unfreeze.  No-op if not paused."""
        if not self._paused:
            return

        pause_duration = time.monotonic() - self._pause_time
        self._animator._start_time  += pause_duration
        self._last_walk_tick        += pause_duration
        # Keep jump timing consistent across a pause.
        self._jump_last_draw_time   += pause_duration
        self._paused = False

        self._overlay.start()

        if self._state == State.WALK:
            fps = self._anims[self._walk_anim_name].fps
            self._walk_timer.start(max(1, int(1000 / fps)))
        elif self._state == State.IDLE:
            if self._wander_remaining_ms > 0:
                self._wander_timer.start(self._wander_remaining_ms)
            if self._sleep_remaining_ms > 0:
                self._sleep_timer.start(self._sleep_remaining_ms)

    # ------------------------------------------------------------------
    # State entries
    # ------------------------------------------------------------------

    def _enter_idle(self) -> None:
        self._state = State.IDLE
        self._walk_timer.stop()
        self._animator.set_animation(self._anims["idle"])

        pause_ms = int(
            random.uniform(config.WANDER_PAUSE_MIN, config.WANDER_PAUSE_MAX) * 1000
        )
        self._wander_timer.start(pause_ms)
        self._sleep_timer.start(int(config.SLEEP_AFTER_IDLE_SECONDS * 1000))

    def _start_wander(self) -> None:
        """Wander timer fired — walk to the opposite half of the screen."""
        if self._state != State.IDLE or self._paused:
            return
        lo, hi = self._ground_x_range()
        mid = (lo + hi) / 2.0
        if self._x >= mid:
            target_x = random.uniform(lo, mid - 1)
        else:
            target_x = random.uniform(mid + 1, hi)
        self._start_walk(target_x=target_x)

    def _start_walk(
        self,
        target_x: float,
        on_arrive: Callable[[], None] | None = None,
    ) -> None:
        self._wander_timer.stop()
        self._sleep_timer.stop()

        self._state = State.WALK
        self._walk_target_x = target_x
        self._walk_arrive_cb = on_arrive
        self._last_walk_tick = time.monotonic()

        self._walk_anim_name = random.choice(["run", "run2"])
        self._walk_speed_pps = (
            config.RUN_SPEED_PPS
            if self._walk_anim_name == "run"
            else config.RUN2_SPEED_PPS
        )
        self._facing_left = target_x < self._x
        self._animator.set_animation(self._anims[self._walk_anim_name])

        fps = self._anims[self._walk_anim_name].fps
        self._walk_timer.start(max(1, int(1000 / fps)))

    def _enter_sleep(self) -> None:
        """Sleep timer fired — walk to a random corner then sit."""
        if self._state not in (State.IDLE,) or self._paused:
            return
        log.debug("Pet going to sleep.")
        self._wander_timer.stop()
        self._sleep_timer.stop()
        self._start_walk(target_x=self._random_corner_x(), on_arrive=self._sit_down)

    def _sit_down(self) -> None:
        """Arrive callback for the sleep walk — start the sit animation."""
        self._wander_timer.stop()
        self._sleep_timer.stop()
        self._walk_timer.stop()
        self._state = State.SLEEP
        self._animator.set_animation(
            self._anims["sit"],
            on_finished=self._on_sit_finished,
        )

    def _on_sit_finished(self) -> None:
        # Animator holds the last frame automatically; nothing to do.
        pass

    def _enter_wake(self) -> None:
        """Stand up (reverse sit), then either jump or walk to centre."""
        log.debug("Pet waking up (jump_pending=%s).", self._jump_pending_after_wake)
        self._state = State.IDLE   # block re-entry from other triggers
        self._animator.set_animation(
            self._anims["sit"],
            on_finished=self._after_wake,
        )
        self._animator.reverse = True

    def _after_wake(self) -> None:
        """Called when the stand-up animation finishes."""
        if self._jump_pending_after_wake:
            self._jump_pending_after_wake = False
            self._enter_jump(fps_scale=self._jump_pending_fps_scale)
        else:
            self._walk_to_centre()

    def _walk_to_centre(self) -> None:
        rect = win_utils.usable_rect()
        self._start_walk(target_x=float(rect.center().x()))

    def _enter_jump(self, fps_scale: float = 1.0) -> None:
        """Interrupt IDLE or WALK, play jump at fps × fps_scale, then restore.

        Speed compression technique
        ---------------------------
        Animator.elapsed() = now − _start_time.
        To make it run fps_scale× faster without changing Animation data,
        each _draw call back-shifts _start_time by  dt × (fps_scale − 1).
        This makes apparent elapsed time grow fps_scale× faster than real
        time, so frame = int(apparent_elapsed / spf) advances faster.
        The correction is applied only while state == JUMP.
        """
        log.debug("Pet jumping (fps_scale=%.2f).", fps_scale)
        self._pre_jump_state      = self._state
        self._last_jump_time      = time.monotonic()
        self._jump_start_time     = self._last_jump_time
        self._jump_fps_scale      = max(0.1, fps_scale)
        self._jump_last_draw_time = self._last_jump_time

        # Suspend movement timers for the duration of the jump.
        self._walk_timer.stop()
        self._wander_timer.stop()
        self._sleep_timer.stop()

        self._state = State.JUMP
        self._animator.set_animation(
            self._anims["jump"],
            on_finished=self._on_jump_finished,
        )

    def _on_jump_finished(self) -> None:
        """Jump animation finished — restore pre-jump state."""
        log.debug("Jump finished, restoring state=%s.", self._pre_jump_state)
        prev = self._pre_jump_state

        if prev == State.WALK:
            self._state = State.WALK
            self._last_walk_tick = time.monotonic()
            self._walk_speed_pps = (
                config.RUN_SPEED_PPS
                if self._walk_anim_name == "run"
                else config.RUN2_SPEED_PPS
            ) * config.CLOSE_SPEED_MULTIPLIER
            self._animator.set_animation(self._anims[self._walk_anim_name])
            fps = self._anims[self._walk_anim_name].fps
            self._walk_timer.start(max(1, int(1000 / fps)))
        else:
            self._enter_idle()

    # ------------------------------------------------------------------
    # Walk tick
    # ------------------------------------------------------------------

    def _walk_tick(self) -> None:
        if self._paused:
            return
        now = time.monotonic()
        dt = now - self._last_walk_tick
        self._last_walk_tick = now

        if dt <= 0.0:
            return

        step = self._walk_speed_pps * dt
        dx   = self._walk_target_x - self._x

        if abs(dx) <= step:
            self._x = self._walk_target_x
            cb = self._walk_arrive_cb
            self._walk_arrive_cb = None
            self._enter_idle()
            if cb is not None:
                cb()
            return

        self._x += step if dx > 0 else -step
        self._x = self._clamp_x(self._x)

    # ------------------------------------------------------------------
    # IdleMonitor callbacks
    # ------------------------------------------------------------------

    def _on_user_idle(self) -> None:
        # Sleep timer already running from _enter_idle(); nothing extra needed.
        pass

    def _on_user_active(self) -> None:
        """User resumed input — wake if sleeping, reset sleep timer otherwise."""
        if self._state == State.SLEEP:
            self._enter_wake()
        elif self._state == State.IDLE:
            self._sleep_timer.start(int(config.SLEEP_AFTER_IDLE_SECONDS * 1000))

    # ------------------------------------------------------------------
    # Draw
    # ------------------------------------------------------------------

    def _jump_y_offset(self) -> float:
        """Extra upward pixels during the airborne frames (sine curve).

        Only active when JUMP_EXTRA_HEIGHT_FRACTION > 0.  Returns 0
        outside the airborne frame range so crouch and landing frames
        sit exactly on the same baseline as idle — no pop.
        """
        if config.JUMP_EXTRA_HEIGHT_FRACTION <= 0.0:
            return 0.0

        jump_anim = self._anims["jump"]
        spf       = jump_anim.spf
        elapsed   = time.monotonic() - self._jump_start_time
        frame_idx = min(int(elapsed / spf), jump_anim.frame_count - 1)

        first = config.JUMP_AIRBORNE_FIRST
        last  = config.JUMP_AIRBORNE_LAST
        if frame_idx < first or frame_idx > last:
            return 0.0

        airborne_frames = last - first + 1
        sub_frame = (elapsed - first * spf) / (airborne_frames * spf)
        sub_frame = max(0.0, min(1.0, sub_frame))

        amplitude = jump_anim.frame_h * config.JUMP_EXTRA_HEIGHT_FRACTION
        return amplitude * math.sin(math.pi * sub_frame)

    def _draw(self, painter: QPainter) -> None:
        """Paint the current frame anchored by bottom-center at (x, y).

        When JUMP is active and fps_scale > 1, back-shifts the animator's
        _start_time by dt × (fps_scale − 1) each frame so the animation
        plays fps_scale× faster than real time.
        """
        # --- speed compression for fast-jump (close events) -----------
        if self._state == State.JUMP and self._jump_fps_scale != 1.0:
            now = time.monotonic()
            dt  = now - self._jump_last_draw_time
            self._jump_last_draw_time = now
            if dt > 0.0:
                # Back-shift start_time so elapsed grows fps_scale× faster.
                self._animator._start_time -= dt * (self._jump_fps_scale - 1.0)
                # Keep _jump_start_time in sync for _jump_y_offset.
                self._jump_start_time      -= dt * (self._jump_fps_scale - 1.0)

        px     = self._animator.current_pixmap(mirror=self._facing_left)
        draw_y = self._y
        if self._state == State.JUMP:
            draw_y -= self._jump_y_offset()

        draw_bottom_center(painter, px, self._x, draw_y)

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _ground_y(self) -> float:
        return float(win_utils.usable_rect().bottom() - config.GROUND_MARGIN)

    def _ground_x_range(self) -> tuple[float, float]:
        rect   = win_utils.usable_rect()
        half_w = self._anims["idle"].frame_w / 2.0
        return float(rect.left()) + half_w, float(rect.right()) - half_w

    def _clamp_x(self, x: float) -> float:
        lo, hi = self._ground_x_range()
        return max(lo, min(x, hi))

    def _random_corner_x(self) -> float:
        lo, hi = self._ground_x_range()
        if random.choice([True, False]):
            return random.uniform(lo, lo + config.SLEEP_CORNER_ZONE)
        return random.uniform(hi - config.SLEEP_CORNER_ZONE, hi)

    def _reset_to_ground_centre(self) -> None:
        rect   = win_utils.usable_rect()
        self._x = float(rect.center().x())
        self._y = self._ground_y()
