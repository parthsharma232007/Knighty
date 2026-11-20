"""config.py — all tunable constants for Knighty.

Dependency-free: nothing from other project modules is imported here.
Path resolution works from source and from a PyInstaller one-file bundle.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Base path — source or PyInstaller bundle
# ---------------------------------------------------------------------------

def _base_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass is not None:
        return Path(meipass)
    return Path(__file__).resolve().parent


BASE_DIR: Path = _base_dir()

# ---------------------------------------------------------------------------
# Asset paths
# ---------------------------------------------------------------------------

ASSETS_DIR:      Path = BASE_DIR / "assets" / "knight"
FRAMES_DIR:      Path = ASSETS_DIR / "frames"
ANIMATIONS_JSON: Path = ASSETS_DIR / "animations.json"

# User-writable settings file (persists size selection across launches).
SETTINGS_FILE: Path = BASE_DIR / "settings.json"

# ---------------------------------------------------------------------------
# Animations — parsed once, shared by all modules via config.ANIMATIONS
# ---------------------------------------------------------------------------

with ANIMATIONS_JSON.open(encoding="utf-8") as _fh:
    ANIMATIONS: dict[str, Any] = json.load(_fh)

# ---------------------------------------------------------------------------
# Display / sprite size
# ---------------------------------------------------------------------------

# Uniform sprite scale (1.0 = native pixel size).
# Changed at runtime by the Size tray menu; saved to settings.json.
DISPLAY_SCALE: float = 1.0

# Human-readable label for the current scale (kept in sync with DISPLAY_SCALE).
SPRITE_SIZE_LABEL: str = "Small"

# Available size options: label → scale factor.
# Changing this dict is the only thing needed to add or rename size steps.
SPRITE_SIZE_MAP: dict[str, float] = {
    "Small":  1.0,
    "Medium": 1.5,
    "Large":  2.0,
}

# ---------------------------------------------------------------------------
# Sprite outline / glow
# ---------------------------------------------------------------------------

# Draw a soft outline/glow behind the sprite to keep the dark knight visible
# on dark wallpapers.
OUTLINE_ENABLED: bool = True

# Colour of the outline as a hex string accepted by QColor("#rrggbb") or
# a named colour.  "white" gives a clean look; "gold" or "#ffdd88" for warmth.
OUTLINE_COLOR: str = "white"

# Half-width of the dilation in logical pixels.  1 = tight outline, 3 = soft glow.
OUTLINE_WIDTH: int = 2

# Opacity of the outline layer (0.0 – 1.0).
OUTLINE_OPACITY: float = 0.55

# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

# Pixels above the taskbar that the pet's feet rest at.
GROUND_MARGIN: int = 48

# ---------------------------------------------------------------------------
# Movement (pixels per second)
# ---------------------------------------------------------------------------

RUN_SPEED_PPS:  float = 120.0   # speed for "run"  animation
RUN2_SPEED_PPS: float = 160.0   # speed for "run2" animation

# ---------------------------------------------------------------------------
# Idle wandering
# ---------------------------------------------------------------------------

# Random pause between wanders (seconds).
WANDER_PAUSE_MIN: float = 2.0
WANDER_PAUSE_MAX: float = 6.0

# ---------------------------------------------------------------------------
# Idle detection (Win32 GetLastInputInfo)
# ---------------------------------------------------------------------------

# Seconds of no keyboard/mouse before the pet is considered idle.
IDLE_TIMEOUT_SECONDS: float = 10.0

# Poll interval for the idle monitor (milliseconds).
IDLE_POLL_MS: int = 500

# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------

# Seconds of continuous idleness before the pet goes to sleep.
SLEEP_AFTER_IDLE_SECONDS: float = 120.0

# Width of the corner zone the knight walks to before sitting down.
SLEEP_CORNER_ZONE: float = 80.0

# ---------------------------------------------------------------------------
# Jump
# ---------------------------------------------------------------------------

# Minimum gap between consecutive jumps (seconds).
JUMP_MIN_GAP_SECONDS: float = 0.8

# Whether window open/close events trigger a jump at all.
REACT_TO_WINDOWS: bool = True

# Extra upward rise (fraction of frame height) over airborne frames only.
JUMP_EXTRA_HEIGHT_FRACTION: float = 0.0

# Airborne frame range (inclusive, 0-indexed).
# Frames 0-2 = crouch, 3-5 = airborne, 6-7 = landing.
JUMP_AIRBORNE_FIRST: int = 3
JUMP_AIRBORNE_LAST:  int = 5

# Walk speed multiplier applied when resuming after a jump.
CLOSE_SPEED_MULTIPLIER: float = 1.0

# ---------------------------------------------------------------------------
# Fullscreen hide
# ---------------------------------------------------------------------------

# Hide the knight (and suppress reactions) whenever a fullscreen app is
# detected on the same monitor the knight is currently standing on.
HIDE_ON_FULLSCREEN: bool = True

# How often to check for a fullscreen app / lock screen (milliseconds).
FULLSCREEN_POLL_MS: int = 1000

# ---------------------------------------------------------------------------
# Screen-lock pause
# ---------------------------------------------------------------------------

# Pause all reactions and animation while the Windows session is locked.
PAUSE_ON_LOCK: bool = True

# ---------------------------------------------------------------------------
# Multi-monitor walking
# ---------------------------------------------------------------------------

# When True the knight walks across all monitors combined (the full virtual
# desktop width).  When False he stays on the primary monitor only.
MULTI_MONITOR: bool = True

# ---------------------------------------------------------------------------
# Window watcher
# ---------------------------------------------------------------------------

WINDOW_POLL_MS: int = 300

# Seconds after start() during which no events are emitted (warmup).
WINDOW_WARMUP_SECONDS: float = 3.0

# Within this window (seconds) only the first open/close event fires.
WINDOW_DEBOUNCE_SECONDS: float = 0.8

# Consecutive missed polls before a close event fires.
WINDOW_CLOSE_CONFIRM_POLLS: int = 2

# Process names (lowercase) never counted as real app windows.
WINDOW_PROCESS_BLOCKLIST: frozenset[str] = frozenset({
    "explorer.exe",
    "searchhost.exe",
    "searchapp.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "textinputhost.exe",
    "lockapp.exe",
    "logonui.exe",
    "dwm.exe",
    "applicationframehost.exe",
    "systemsettings.exe",
    "peopleapp.exe",
})

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Directory where the rotating log file is written.
# Defaults to BASE_DIR; override to %APPDATA%\Knighty if you prefer.
LOG_DIR: Path = BASE_DIR

# Log file name.
LOG_FILENAME: str = "knighty.log"

# Maximum size of a single log file before rotation (bytes).
LOG_MAX_BYTES: int = 1 * 1024 * 1024   # 1 MB

# How many rotated backup files to keep (knighty.log.1, .2, …).
LOG_BACKUP_COUNT: int = 3

# ---------------------------------------------------------------------------
# Tray
# ---------------------------------------------------------------------------

TRAY_TOOLTIP: str = "Knighty"
