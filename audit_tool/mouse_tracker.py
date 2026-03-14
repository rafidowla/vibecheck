"""
Mouse click tracker with annotated screenshot capture.

Purpose:
    Listens for global mouse clicks using ``pynput``, captures a screenshot
    of the selected monitor via ``mss`` on each click, and draws a vivid
    crosshair + circle at the click position using ``Pillow``.  The annotated
    screenshot is saved to the session directory.  Supports pause/resume
    and mid-session monitor switching.

Inputs:
    ``start(session_dir, monitor_index)``
        session_dir (Path): Where annotated PNGs are saved.
        monitor_index (int): 1-based index into ``mss().monitors``.

Outputs:
    ``stop() -> list[ClickRecord]``
        Returns metadata for every click captured during the session.

Error Behaviour:
    - Raises ``RuntimeError`` if ``stop()`` is called before ``start()``.
    - Logs and skips individual screenshot failures without crashing the
      listener.

Side Effects:
    - Installs a global mouse listener (requires macOS Accessibility
      permission).
    - Captures screenshots (requires macOS Screen Recording permission).
    - Writes ``click_NNN.png`` files to disk.

Determinism: Nondeterministic (event-driven).
Idempotency: No — each session is unique.
Thread Safety: The pynput listener runs on its own thread; screenshot I/O
    is serialised via the listener callback.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mss
import mss.tools
from PIL import Image, ImageDraw
from pynput import mouse

from audit_tool.config import (
    CLICK_MARKER_COLOR,
    CLICK_MARKER_RADIUS,
    CLICK_MARKER_WIDTH,
    SCREENSHOT_QUALITY,
)

logger = logging.getLogger(__name__)


@dataclass
class ClickRecord:
    """Metadata for a single captured click event.

    Attributes:
        index: Sequential click number (0-based).
        timestamp: Unix epoch time of the click.
        x: Absolute screen X coordinate.
        y: Absolute screen Y coordinate.
        screenshot_path: Path to the annotated PNG file.
        monitor_index: Which monitor was being captured.
    """

    index: int
    timestamp: float
    x: int
    y: int
    screenshot_path: Path
    monitor_index: int


class MouseTracker:
    """Tracks mouse clicks and captures annotated screenshots.

    Supports pause/resume and mid-session monitor switching.

    Typical usage::

        tracker = MouseTracker()
        tracker.start(session_dir, monitor_index=1)
        # ... user clicks around ...
        tracker.pause()
        # ... break ...
        tracker.resume()
        tracker.switch_monitor(2)  # switch to another monitor
        # ... more clicks ...
        clicks = tracker.stop()
    """

    def __init__(self) -> None:
        self._clicks: list[ClickRecord] = []
        self._listener: Optional[mouse.Listener] = None
        self._session_dir: Optional[Path] = None
        self._monitor_index: int = 1
        self._click_counter: int = 0
        self._active: bool = False
        self._paused: bool = False
        self._sct: Optional[mss.mss] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_tracking(self) -> bool:
        """Whether the tracker is actively listening for clicks."""
        return self._active

    @property
    def is_paused(self) -> bool:
        """Whether the tracker is paused."""
        return self._paused

    @property
    def click_count(self) -> int:
        """Number of clicks captured so far."""
        return self._click_counter

    @property
    def monitor_index(self) -> int:
        """Currently tracked monitor index (1-based)."""
        return self._monitor_index

    def start(self, session_dir: Path, monitor_index: int = 1) -> None:
        """Begin listening for mouse clicks.

        Args:
            session_dir: Directory where annotated screenshots are saved.
            monitor_index: 1-based ``mss`` monitor index to capture.

        Raises:
            RuntimeError: If already tracking.

        Side Effects:
            Installs a global mouse listener.
        """
        if self._active:
            raise RuntimeError("MouseTracker is already active.")

        self._session_dir = session_dir
        self._monitor_index = monitor_index
        self._clicks = []
        self._click_counter = 0
        self._active = True
        self._paused = False
        self._sct = mss.mss()

        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()

    def pause(self) -> None:
        """Pause click tracking.  Clicks are ignored until resumed."""
        if not self._active:
            raise RuntimeError("MouseTracker is not active.")
        self._paused = True
        logger.info("Mouse tracking paused.")

    def resume(self) -> None:
        """Resume click tracking after a pause."""
        if not self._active:
            raise RuntimeError("MouseTracker is not active.")
        self._paused = False
        logger.info("Mouse tracking resumed.")

    def switch_monitor(self, monitor_index: int) -> None:
        """Switch the captured monitor mid-session.

        Args:
            monitor_index: New 1-based ``mss`` monitor index.

        Side Effects:
            Subsequent screenshots will capture the new monitor.
        """
        self._monitor_index = monitor_index
        logger.info("Switched tracking to monitor %d.", monitor_index)

    def stop(self) -> list[ClickRecord]:
        """Stop listening and return all click records.

        Returns:
            List of ``ClickRecord`` objects in chronological order.

        Raises:
            RuntimeError: If not currently tracking.

        Side Effects:
            Removes the global mouse listener.
        """
        if not self._active:
            raise RuntimeError("MouseTracker is not active.")

        if self._listener is not None:
            self._listener.stop()
            self._listener = None

        if self._sct is not None:
            self._sct.close()
            self._sct = None

        self._active = False
        self._paused = False
        return list(self._clicks)

    def get_clicks(self) -> list[ClickRecord]:
        """Return a copy of click records captured so far (non-destructive)."""
        return list(self._clicks)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_click(
        self,
        x: int,
        y: int,
        button: mouse.Button,
        pressed: bool,
    ) -> None:
        """Callback fired on every mouse click event.

        Only processes *press* events (ignores releases) to avoid duplicates.
        Skips capture when paused.
        """
        if not pressed or not self._active or self._paused:
            return

        try:
            screenshot_path = self._capture_and_annotate(x, y)
            record = ClickRecord(
                index=self._click_counter,
                timestamp=time.time(),
                x=x,
                y=y,
                screenshot_path=screenshot_path,
                monitor_index=self._monitor_index,
            )
            self._clicks.append(record)
            self._click_counter += 1
        except Exception:
            logger.exception("Failed to capture screenshot for click at (%d, %d)", x, y)

    def _capture_and_annotate(self, click_x: int, click_y: int) -> Path:
        """Capture a screenshot, draw a click marker, and save.

        Args:
            click_x: Absolute X position of the click.
            click_y: Absolute Y position of the click.

        Returns:
            Path to the saved annotated PNG.
        """
        assert self._sct is not None
        assert self._session_dir is not None

        monitor = self._sct.monitors[self._monitor_index]

        # Capture the raw screenshot
        raw = self._sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        # Translate absolute screen coords → image-local coords
        local_x = click_x - monitor["left"]
        local_y = click_y - monitor["top"]

        # Draw the click marker
        draw = ImageDraw.Draw(img)
        radius = CLICK_MARKER_RADIUS
        width = CLICK_MARKER_WIDTH
        color = CLICK_MARKER_COLOR

        # Outer circle
        draw.ellipse(
            [local_x - radius, local_y - radius, local_x + radius, local_y + radius],
            outline=color,
            width=width,
        )

        # Inner crosshair
        cross_len = radius + 8
        draw.line(
            [local_x - cross_len, local_y, local_x + cross_len, local_y],
            fill=color,
            width=width,
        )
        draw.line(
            [local_x, local_y - cross_len, local_x, local_y + cross_len],
            fill=color,
            width=width,
        )

        # Save
        filename = f"click_{self._click_counter:04d}.png"
        filepath = self._session_dir / filename
        img.save(str(filepath), "PNG", optimize=True)
        return filepath
