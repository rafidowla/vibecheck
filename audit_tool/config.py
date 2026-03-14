"""
Configuration module for VibeCheck.

Purpose:
    Centralises all runtime settings, directory management, and environment
    variable loading.  Every other module imports its tunables from here so
    there is a single source of truth for paths, API keys, and recording
    parameters.

Side Effects:
    - Reads the filesystem for a `.env` file via ``python-dotenv``.
    - Creates the output directory tree on disk when ``create_session_dir()``
      is called.

Determinism: Deterministic.
Idempotency: Safe to call repeatedly — ``create_session_dir`` generates a
    new timestamped folder each invocation.
Thread Safety: Yes — all values are read-only after module load.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: load .env from the project root (two levels up from this file)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# OpenRouter settings
# ---------------------------------------------------------------------------
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.getenv(
    "OPENROUTER_MODEL", "qwen/qwen2.5-vl-72b-instruct"
)
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1/chat/completions"

# ---------------------------------------------------------------------------
# Whisper settings
# ---------------------------------------------------------------------------
WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL", "base")

# ---------------------------------------------------------------------------
# Audio recording settings
# ---------------------------------------------------------------------------
AUDIO_SAMPLE_RATE: int = 16_000  # 16 kHz — Whisper's native rate
AUDIO_CHANNELS: int = 1  # mono

# ---------------------------------------------------------------------------
# Screenshot settings
# ---------------------------------------------------------------------------
SCREENSHOT_QUALITY: int = 85  # JPEG quality for annotated screenshots
CLICK_MARKER_RADIUS: int = 22  # radius of the highlight circle (px)
CLICK_MARKER_COLOR: str = "#FF2D2D"  # vivid red
CLICK_MARKER_WIDTH: int = 3  # stroke width of the circle

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR: Path = Path(
    os.getenv("OUTPUT_DIR", "~/Downloads/vibecheck-output")
).expanduser()


def create_session_dir() -> Path:
    """Create a temporary session directory for an in-progress recording.

    The directory uses a ``_recording_<timestamp>`` naming scheme to signal
    that it is temporary.  After report generation, ``_rename_session_dir``
    replaces it with a clean AI-generated descriptive name.

    Returns:
        Path: Absolute path to the newly created session folder, e.g.
              ``~/Downloads/vibecheck-output/_recording_20260313-140045/``.

    Side Effects:
        Creates the directory (and parents) on disk.

    Determinism: Deterministic (timestamp-based).
    Idempotency: Each call creates a new directory.
    """
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = DEFAULT_OUTPUT_DIR / f"_recording_{timestamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir
