"""
Audio transcription module using whisper.cpp (subprocess).

Purpose:
    Transcribes a WAV file recorded during an audit session by invoking the
    ``whisper-cpp`` CLI (installed via Homebrew).  Returns timestamped
    segments so the report generator can correlate spoken observations with
    click timestamps.

    Using the whisper.cpp C++ binary avoids Python version-compatibility
    issues (openai-whisper / faster-whisper do not build on Python 3.14).

Inputs:
    ``transcribe(wav_path, model_size) -> list[TranscriptSegment]``
        wav_path (Path): Absolute path to a 16 kHz mono WAV file.
        model_size (str): Whisper model to use (base, small, medium, large-v3).

Outputs:
    A list of ``TranscriptSegment`` dataclass instances, each containing
    ``start`` (float, seconds), ``end`` (float, seconds), and ``text`` (str).

Error Behaviour:
    - Raises ``FileNotFoundError`` if ``wav_path`` does not exist.
    - Raises ``RuntimeError`` if whisper-cpp is not installed or transcription
      fails.

Side Effects:
    - Spawns a subprocess (``whisper-cpp``).
    - Reads/writes temporary files in the session directory.
    - Downloads the model file on first use if not present.

Performance:
    - "base" model: ~6s per minute of audio on modern macOS.
    - "medium" model: ~25s per minute.
    - "large-v3" model: ~50s per minute.
    - Blocking call — should be run from a background thread.

Determinism: Mostly deterministic (minor variance across runs).
Idempotency: Yes — same input produces the same transcript.
Thread Safety: Yes — each call spawns an independent subprocess.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from audit_tool.config import WHISPER_MODEL_SIZE

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MODELS_DIR = _PROJECT_ROOT / "models"

# Available models with descriptions and estimated peak RAM.
WHISPER_MODELS = {
    "base":        "Fast, English-focused (~500 MB RAM, ~6s/min)",
    "small":       "Decent multilingual (~900 MB RAM, ~12s/min)",
    "medium-q5":   "Multilingual, quantised (~1 GB RAM, ~20s/min)",
    "medium":      "Multilingual, full (~2.5 GB RAM, ~25s/min)",
    "large-v3-q5": "Best quality, quantised (~2 GB RAM, ~40s/min)",
    "large-v3":    "Best quality, full (~4.5 GB RAM, ~50s/min)",
}

# Maps picker keys to (ggml filename, is_english_only)
_MODEL_FILES: dict[str, tuple[str, bool]] = {
    "base":        ("ggml-base.en.bin",          True),
    "small":       ("ggml-small.en.bin",         True),
    "medium-q5":   ("ggml-medium-q5_0.bin",      False),
    "medium":      ("ggml-medium.bin",           False),
    "large-v3-q5": ("ggml-large-v3-q5_0.bin",    False),
    "large-v3":    ("ggml-large-v3.bin",          False),
}


@dataclass
class TranscriptSegment:
    """A single timestamped segment of transcribed speech.

    Attributes:
        start: Start time in seconds from the beginning of the recording.
        end: End time in seconds from the beginning of the recording.
        text: The transcribed text for this segment.
    """

    start: float
    end: float
    text: str


def _find_whisper_binary() -> str:
    """Locate the whisper-cpp binary on the system.

    Prefers the ARM64 (Apple Silicon) binary at ``/opt/homebrew/bin/``
    for Metal GPU acceleration.  Falls back to the Intel binary at
    ``/usr/local/bin/`` or anywhere on PATH.

    Returns:
        Absolute path to the whisper executable.

    Raises:
        RuntimeError: If the binary is not found.
    """
    # Prefer native ARM64 binary (Metal GPU) over Intel/Rosetta (CPU-only)
    for candidate in [
        "/opt/homebrew/bin/whisper-cli",
        "/opt/homebrew/bin/whisper-cpp",
        "/usr/local/bin/whisper-cli",
        "/usr/local/bin/whisper-cpp",
    ]:
        if Path(candidate).exists():
            return candidate

    # Fall back to PATH search
    for name in ["whisper-cli", "whisper-cpp"]:
        found = shutil.which(name)
        if found:
            return found

    raise RuntimeError(
        "whisper-cli not found. Install it with: brew install whisper-cpp"
    )


def _resolve_model_path(model_size: str) -> Path:
    """Locate or download the Whisper GGML model file.

    For ``base`` and ``small``, uses the English-only variant (``*.en.bin``)
    for speed.  For ``medium`` and ``large-v3``, uses the multilingual
    variant (``*.bin``) for better mixed-language support.

    Args:
        model_size: One of 'base', 'small', 'medium', 'large-v3'.

    Returns:
        Path to the model file.

    Raises:
        RuntimeError: If the model file cannot be found or downloaded.
    """
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)

    # Look up the model filename from the mapping
    if model_size not in _MODEL_FILES:
        raise RuntimeError(
            f"Unknown model '{model_size}'. "
            f"Available: {', '.join(_MODEL_FILES.keys())}"
        )
    model_filename, _is_english = _MODEL_FILES[model_size]

    model_path = _MODELS_DIR / model_filename

    if model_path.exists():
        return model_path

    # Check Homebrew model locations
    for brew_dir in [
        Path("/usr/local/share/whisper-cpp/models"),
        Path("/opt/homebrew/share/whisper-cpp/models"),
    ]:
        brew_path = brew_dir / model_filename
        if brew_path.exists():
            return brew_path

    # Try to download the model
    download_url = (
        f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{model_filename}"
    )
    logger.info("Downloading Whisper model '%s' → %s", model_size, model_path)

    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError(
            f"Whisper model not found at {model_path}.\n"
            f"Download it manually:\n"
            f"  curl -L -o {model_path} {download_url}"
        )

    try:
        result = subprocess.run(
            [curl, "-L", "--progress-bar", "-o", str(model_path), download_url],
            capture_output=False,
            timeout=600,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        # Clean up partial download
        if model_path.exists():
            model_path.unlink()
        raise RuntimeError(
            f"Failed to download Whisper model '{model_size}'.\n"
            f"Download it manually:\n"
            f"  curl -L -o {model_path} {download_url}"
        ) from err

    return model_path


def _parse_timestamp(timestamp_str: str) -> float:
    """Parse a whisper-cpp timestamp string like '00:01:23.456' to seconds.

    Args:
        timestamp_str: Timestamp in HH:MM:SS.mmm format.

    Returns:
        Total seconds as a float.
    """
    parts = timestamp_str.strip().split(":")
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    elif len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        return minutes * 60 + seconds
    return 0.0


def _convert_to_16khz_wav(input_path: Path) -> Path:
    """Convert audio to 16kHz mono WAV using ffmpeg if needed.

    Args:
        input_path: Path to the input audio file.

    Returns:
        Path to the converted file (may be the same as input if already valid).
    """
    output_path = input_path.parent / f"{input_path.stem}_16khz.wav"

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("ffmpeg not found — using original audio file as-is.")
        return input_path

    try:
        subprocess.run(
            [
                ffmpeg, "-y",
                "-i", str(input_path),
                "-ar", "16000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return output_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        logger.warning("ffmpeg conversion failed: %s — using original file.", err)
        return input_path


def transcribe(
    wav_path: Path,
    model_size: Optional[str] = None,
) -> list[TranscriptSegment]:
    """Transcribe a WAV file and return timestamped segments.

    Args:
        wav_path: Path to a WAV audio file.
        model_size: Whisper model to use. One of 'base', 'small', 'medium',
            'large-v3'.  Defaults to the ``WHISPER_MODEL`` env var (or 'medium').

    Returns:
        Ordered list of ``TranscriptSegment`` instances.

    Raises:
        FileNotFoundError: If ``wav_path`` does not exist.
        RuntimeError: If whisper-cpp is not installed or transcription fails.

    Side Effects:
        Spawns a subprocess.  May download the model on first use.
        May create a temporary 16kHz WAV conversion.

    Performance:
        Blocking; speed depends on model size (see module docstring).
    """
    wav_path = Path(wav_path)
    if not wav_path.exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")

    if model_size is None:
        model_size = WHISPER_MODEL_SIZE

    binary = _find_whisper_binary()
    model_path = _resolve_model_path(model_size)

    # Ensure 16kHz mono WAV (whisper-cpp requirement)
    converted_path = _convert_to_16khz_wav(wav_path)

    logger.info(
        "Transcribing %s with whisper-cpp (model: %s) …",
        wav_path.name,
        model_size,
    )

    # Determine thread count: use half the logical CPUs (Metal offloads the
    # heavy compute, so fewer CPU threads avoids contention with the GPU).
    import os as _os
    cpu_threads = max(2, _os.cpu_count() // 2)

    try:
        result = subprocess.run(
            [
                binary,
                "-m", str(model_path),
                "-f", str(converted_path),
                "--output-txt",
                "--print-progress",
                "--gpu",          # enable Metal GPU on Apple Silicon (no-op on Intel)
                "--threads", str(cpu_threads),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired as timeout_error:
        raise RuntimeError(
            "Transcription timed out after 10 minutes."
        ) from timeout_error

    if result.returncode != 0:
        logger.error("whisper-cpp stderr: %s", result.stderr)
        raise RuntimeError(
            f"whisper-cpp failed (exit {result.returncode}): {result.stderr[:500]}"
        )

    segments = _parse_whisper_output(result.stdout)

    # Clean up temporary conversion file
    if converted_path != wav_path and converted_path.exists():
        converted_path.unlink()

    logger.info("Transcription complete: %d segments.", len(segments))
    return segments


def _parse_whisper_output(output: str) -> list[TranscriptSegment]:
    """Parse whisper-cpp's stdout into TranscriptSegment objects.

    Whisper-cpp outputs lines like:
        [00:00:00.000 --> 00:00:05.000]  This is the first segment.

    Args:
        output: Raw stdout from the whisper-cpp process.

    Returns:
        Ordered list of ``TranscriptSegment`` instances.
    """
    segments: list[TranscriptSegment] = []

    timestamp_pattern = re.compile(
        r"\[(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})\]\s*(.*)"
    )

    for line in output.splitlines():
        match = timestamp_pattern.match(line.strip())
        if match:
            start_str, end_str, text = match.groups()
            text = text.strip()
            if text:
                segments.append(
                    TranscriptSegment(
                        start=_parse_timestamp(start_str),
                        end=_parse_timestamp(end_str),
                        text=text,
                    )
                )

    return segments
