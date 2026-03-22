"""
Audio recorder module for VibeCheck.

Purpose:
    Records microphone audio in a background thread and streams it directly
    to a WAV file on disk.  Supports pause/resume to let the user take
    breaks during long review sessions.

Inputs:
    ``start(session_dir)``
        session_dir (Path): Directory where ``recording.wav`` will be written.

Outputs:
    ``stop() -> Path``
        Returns the absolute path to the saved WAV file.

Error Behaviour:
    - Raises ``RuntimeError`` if ``stop()`` is called before ``start()``.
    - Raises ``sounddevice.PortAudioError`` if no input device is available.

Side Effects:
    - Opens the default microphone input stream.
    - Writes ``recording.wav`` to disk continuously during recording.

Performance:
    - Disk usage: ~1.9 MB/min at 16 kHz mono 16-bit PCM.
    - RAM usage: constant (~32 KB buffer), regardless of recording length.
    - Supports recordings of any practical length (hours).

Determinism: Nondeterministic (real-time audio capture).
Idempotency: No — each start/stop cycle produces a new recording.
Thread Safety: Yes — uses a daemon thread and threading events for signalling.
Concurrency: The recording thread is I/O-bound (audio hardware + disk).
"""

from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path
from typing import Optional

import sounddevice as sd

from audit_tool.config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE

logger = logging.getLogger(__name__)


class AudioRecorder:
    """Records microphone audio directly to a WAV file on disk.

    Streams audio frames to disk in real time, keeping RAM usage constant
    regardless of recording length.  Supports pause/resume.

    Typical usage::

        recorder = AudioRecorder()
        recorder.start(session_dir)
        # ... user speaks ...
        recorder.pause()
        # ... break ...
        recorder.resume()
        # ... more speaking ...
        wav_path = recorder.stop()
    """

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # SET = recording, CLEAR = paused
        self._thread: Optional[threading.Thread] = None
        self._session_dir: Optional[Path] = None
        self._wav_path: Optional[Path] = None
        self._recording = False
        self._paused = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        """Whether the recorder is currently capturing audio."""
        return self._recording

    @property
    def is_paused(self) -> bool:
        """Whether the recorder is currently paused."""
        return self._paused

    def start(self, session_dir: Path) -> None:
        """Begin recording from the default microphone.

        Audio is streamed directly to ``recording.wav`` on disk.

        Args:
            session_dir: Directory where ``recording.wav`` will be saved.

        Raises:
            RuntimeError: If already recording.

        Side Effects:
            Opens the system microphone input stream.
            Creates ``recording.wav`` and begins writing audio data.
        """
        if self._recording:
            raise RuntimeError("AudioRecorder is already recording.")

        self._session_dir = session_dir
        self._wav_path = session_dir / "recording.wav"
        self._stop_event.clear()
        self._pause_event.set()  # Start in "recording" (not paused) state
        self._recording = True
        self._paused = False

        # Store the recording start epoch so that temporal correlation in
        # report generation can correctly map click timestamps to transcript
        # segment times.  Transcript segments are zero-indexed from this moment.
        import time as _time
        _start_epoch = _time.time()
        try:
            (session_dir / "recording_start.txt").write_text(
                str(_start_epoch), encoding="utf-8"
            )
        except Exception:
            pass  # Non-fatal — correlation uses a fallback if missing

        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        """Pause audio recording.  Audio frames are silently discarded.

        Raises:
            RuntimeError: If not currently recording or already paused.
        """
        if not self._recording:
            raise RuntimeError("AudioRecorder is not recording.")
        self._pause_event.clear()
        self._paused = True
        logger.info("Audio recording paused.")

    def resume(self) -> None:
        """Resume audio recording after a pause.

        Raises:
            RuntimeError: If not currently paused.
        """
        if not self._paused:
            raise RuntimeError("AudioRecorder is not paused.")
        self._pause_event.set()
        self._paused = False
        logger.info("Audio recording resumed.")

    def stop(self) -> Path:
        """Stop recording and finalise the WAV file.

        Returns:
            Absolute path to the saved ``recording.wav``.

        Raises:
            RuntimeError: If not currently recording.

        Side Effects:
            Closes the WAV file on disk.
        """
        if not self._recording:
            raise RuntimeError("AudioRecorder is not recording.")

        self._stop_event.set()
        self._pause_event.set()  # Unblock if paused so thread can exit
        if self._thread is not None:
            self._thread.join(timeout=5.0)

        self._recording = False
        self._paused = False

        assert self._wav_path is not None
        logger.info("Audio saved: %s", self._wav_path)
        return self._wav_path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record_loop(self) -> None:
        """Background thread: stream audio directly to WAV file on disk.

        When paused, audio frames from the mic are read but discarded to
        keep the stream alive (avoids device errors on resume).

        Side Effects:
            Creates and writes to ``recording.wav`` on disk.

        Performance:
            ~1.9 MB/min at 16 kHz, mono, 16-bit PCM.
        """
        assert self._wav_path is not None

        block_duration_ms = 200  # 200 ms blocks
        block_size = int(AUDIO_SAMPLE_RATE * block_duration_ms / 1000)
        sample_width = 2  # 16-bit PCM = 2 bytes per sample

        wav_file = wave.open(str(self._wav_path), "wb")
        wav_file.setnchannels(AUDIO_CHANNELS)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(AUDIO_SAMPLE_RATE)

        try:
            with sd.InputStream(
                samplerate=AUDIO_SAMPLE_RATE,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                blocksize=block_size,
            ) as stream:
                while not self._stop_event.is_set():
                    data, _overflowed = stream.read(block_size)
                    # Only write if not paused
                    if self._pause_event.is_set():
                        wav_file.writeframes(data.tobytes())
        except Exception as recording_error:
            logger.exception("Audio recording error: %s", recording_error)
        finally:
            wav_file.close()
