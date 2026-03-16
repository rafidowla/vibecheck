"""
Configuration module for VibeCheck.

Purpose:
    Centralises all runtime settings, directory management, and environment
    variable loading.  Every other module imports its tunables from here so
    there is a single source of truth for paths, API keys, and recording
    parameters.

Inputs:
    Reads ``.env`` from the project root at import time.

Outputs / Exports:
    - ``ProcessMode`` enum — selects the AI prompt and output format.
    - ``JIRA_CONFIG`` — a populated ``JiraConfig`` dataclass, or ``None``
      when Jira credentials are not configured.
    - All recording and API constants.

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
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: load .env from the project root (two levels up from this file)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# OpenRouter settings
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Process mode
# ---------------------------------------------------------------------------


class ProcessMode(str, Enum):
    """Selects the AI prompt style and output document structure.

    Attributes:
        QA: Produces a structured bug/task list optimised for AI coding agents
            and Jira tickets.  Each finding becomes an independently actionable
            task with implementation steps and acceptance criteria.
        DOCUMENTATION: Produces a step-by-step SOP / tutorial document
            describing how to use the application.  Language is instructional
            ("Click the…", "Enter your…") rather than fix-oriented.
    """

    QA = "qa"
    DOCUMENTATION = "documentation"


# ---------------------------------------------------------------------------
# OpenRouter settings
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL: str = os.getenv(
    "OPENROUTER_MODEL", "google/gemini-2.5-flash"
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
SCREENSHOT_QUALITY: int = 85   # PNG quality for annotated screenshots

# Click marker — modern ripple style (works on any background colour)
# Layer 1: white outer ring  → always visible on dark backgrounds
# Layer 2: indigo filled circle → always visible on light backgrounds
# Layer 3: white centre dot  → pinpoints the exact pixel on any surface
CLICK_MARKER_RADIUS: int = 22          # radius of the filled indicator circle (logical px)
CLICK_MARKER_OUTER: str = "#FFFFFF"    # outer ring colour (white)
CLICK_MARKER_FILL: str = "#6366F1"     # filled circle colour (indigo)
CLICK_MARKER_CENTER: str = "#FFFFFF"   # centre dot colour (white)
CLICK_MARKER_OUTER_GAP: int = 4        # gap (px) between outer ring and filled circle
CLICK_MARKER_WIDTH: int = 3            # stroke width of the outer ring

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR: Path = Path(
    os.getenv("OUTPUT_DIR", "~/Downloads/vibecheck-output")
).expanduser()


# ---------------------------------------------------------------------------
# Jira integration (all optional — set to None when unconfigured)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JiraConfig:
    """Immutable Jira connection configuration.

    Attributes:
        base_url: Jira Cloud root URL, e.g. ``https://yourorg.atlassian.net``.
        email: Atlassian account email used for Basic Auth.
        api_token: Jira API token (never a password).
        project_key: Project key, e.g. ``PROJ``.
        issue_type: Issue type name, default ``Task``.
    """

    base_url: str
    email: str
    api_token: str
    project_key: str
    issue_type: str = "Task"


def _load_jira_config() -> Optional[JiraConfig]:
    """Load Jira config from environment variables.

    Returns:
        A populated ``JiraConfig`` if all required vars are present,
        otherwise ``None``.

    Side Effects:
        Reads environment variables (no I/O beyond that).

    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Yes — read-only.
    """
    base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    email = os.getenv("JIRA_EMAIL", "")
    api_token = os.getenv("JIRA_API_TOKEN", "")
    project_key = os.getenv("JIRA_PROJECT_KEY", "")
    issue_type = os.getenv("JIRA_ISSUE_TYPE", "Task")

    if base_url and email and api_token and project_key:
        return JiraConfig(
            base_url=base_url,
            email=email,
            api_token=api_token,
            project_key=project_key,
            issue_type=issue_type,
        )
    return None


JIRA_CONFIG: Optional[JiraConfig] = _load_jira_config()


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
