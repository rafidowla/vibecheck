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
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap: resolve base path for bundled resources
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resource_path(relative: str) -> Path:
    """Resolve a project-relative path for both development and PyInstaller.

    Purpose:
        When running inside a PyInstaller bundle, data files are extracted
        to a temporary ``sys._MEIPASS`` directory.  In normal development,
        paths are relative to the project root.  This function abstracts
        the difference so every module can use ``resource_path('prompts')``
        regardless of execution context.

    Args:
        relative: A path string relative to the project root, e.g.
            ``"prompts"`` or ``".env"`` or ``"bin/whisper-cli"``.

    Returns:
        Absolute ``Path`` to the resource.

    Side Effects: None.
    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Yes — read-only.
    """
    # PyInstaller sets sys._MEIPASS to the temp extraction directory
    base = getattr(sys, "_MEIPASS", None)
    if base is not None:
        return Path(base) / relative
    return _PROJECT_ROOT / relative


# ---------------------------------------------------------------------------
# Bootstrap: load .env from the best available location
# ---------------------------------------------------------------------------
# Uses a manual parser to directly set os.environ — more reliable than
# python-dotenv in PyInstaller .app bundles on macOS where load_dotenv()
# can silently fail due to import ordering or sandboxing.
#
# Search order (first found wins):
#   1. ~/.vibecheck/.env       — user config, always writable
#   2. Next to the executable  — for portable installs
#   3. Project root            — development mode
#   4. PyInstaller bundle      — bundled .env.example fallback

_USER_CONFIG_DIR = Path.home() / ".vibecheck"
_ENV_SEARCH_PATHS = [
    _USER_CONFIG_DIR / ".env",
    Path(sys.executable).parent / ".env",
    _PROJECT_ROOT / ".env",
    resource_path(".env"),
]

_startup_log_lines: list[str] = []
_startup_log_lines.append(f"VibeCheck startup | sys.executable={sys.executable}")
_startup_log_lines.append(f"HOME={Path.home()}")
_startup_log_lines.append(f"_MEIPASS={getattr(sys, '_MEIPASS', 'N/A')}")


def _load_env_file(env_path: Path) -> int:
    """Parse a .env file and write variables directly into os.environ.

    Purpose:
        Bypasses python-dotenv to avoid PyInstaller .app bundle issues where
        load_dotenv() silently fails on macOS.

    Args:
        env_path: Absolute path to the .env file to parse.

    Returns:
        The number of environment variables successfully set.

    Side Effects:
        Writes values directly to os.environ.

    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Yes (os.environ writes are atomic on CPython).
    """
    count = 0
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        key = key.strip()
        value = raw_value.strip().strip('"').strip("'")
        if key and value:
            os.environ[key] = value
            count += 1
    return count


_env_loaded = False
for _env_path in _ENV_SEARCH_PATHS:
    _exists = _env_path.exists()
    _startup_log_lines.append(f"  checking: {_env_path} -> {'EXISTS' if _exists else 'missing'}")
    if _exists:
        try:
            _n = _load_env_file(_env_path)
            _startup_log_lines.append(f"  loaded {_n} vars from {_env_path}")
            _env_loaded = True
        except Exception as _exc:
            _startup_log_lines.append(f"  ERROR loading {_env_path}: {_exc}")
        break

if not _env_loaded:
    _example = resource_path(".env.example")
    _startup_log_lines.append(f"  no .env found, checking example: {_example}")
    if _example.exists():
        _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _dest = _USER_CONFIG_DIR / ".env"
        import shutil
        shutil.copy2(str(_example), str(_dest))
        try:
            _n = _load_env_file(_dest)
            _startup_log_lines.append(f"  created ~/.vibecheck/.env from example, loaded {_n} vars")
        except Exception as _exc:
            _startup_log_lines.append(f"  ERROR loading created .env: {_exc}")

_startup_log_lines.append(
    "  OPENROUTER_API_KEY after load: "
    + ("SET (len=" + str(len(os.environ.get("OPENROUTER_API_KEY", ""))) + ")"
       if os.environ.get("OPENROUTER_API_KEY") else "EMPTY")
)

# Write startup log for diagnostics
try:
    _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (_USER_CONFIG_DIR / "startup.log").write_text(
        "\n".join(_startup_log_lines) + "\n", encoding="utf-8"
    )
except Exception:
    pass  # Non-fatal — diagnostics only



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
# NOTE: Read at call-time (not module-import time) to handle PyInstaller bundles
# where the .env may be loaded after some imports resolve.

def _get_api_key() -> str:
    """Return the OpenRouter API key from the environment.

    Returns:
        The API key string, or an empty string if not configured.

    Side Effects: None.
    Thread Safety: Yes — read-only os.environ access.
    """
    return os.getenv("OPENROUTER_API_KEY", "")


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
