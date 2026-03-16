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
    CLICK_MARKER_CENTER,
    CLICK_MARKER_FILL,
    CLICK_MARKER_OUTER,
    CLICK_MARKER_OUTER_GAP,
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

        On HiDPI / Retina displays, ``pynput`` reports logical-point coordinates
        while ``mss`` captures at physical-pixel resolution (typically 2× on
        Apple Silicon / Retina).  This method derives the physical-to-logical
        scale factor from the ratio of the raw capture dimensions to the logical
        monitor dimensions reported by ``mss``.  No platform-specific branches
        are needed — on standard 1× displays the scale factors equal 1.0.

        Args:
            click_x: Absolute X position of the click in *logical* screen points.
            click_y: Absolute Y position of the click in *logical* screen points.

        Returns:
            Path to the saved annotated PNG.

        Side Effects:
            Writes a PNG file to the session directory.
        """
        assert self._sct is not None
        assert self._session_dir is not None

        monitor = self._sct.monitors[self._monitor_index]

        # Capture the raw screenshot (dimensions are in physical pixels).
        raw = self._sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        # Compute scale factors: physical pixels per logical point.
        # On Retina/HiDPI this is typically 2.0; on standard displays it is 1.0.
        # raw.size contains (physical_width, physical_height).
        # monitor["width"]/monitor["height"] are in logical points.
        logical_width: int = monitor["width"]
        logical_height: int = monitor["height"]
        physical_width, physical_height = raw.size

        scale_x: float = physical_width / logical_width if logical_width > 0 else 1.0
        scale_y: float = physical_height / logical_height if logical_height > 0 else 1.0

        # Translate absolute logical coords → physical pixel coords within the image.
        local_x = int((click_x - monitor["left"]) * scale_x)
        local_y = int((click_y - monitor["top"]) * scale_y)

        # Clamp to image bounds to prevent drawing outside the canvas.
        local_x = max(0, min(local_x, physical_width - 1))
        local_y = max(0, min(local_y, physical_height - 1))

        # Scale the marker radius/width proportionally so it looks the same
        # visual size regardless of DPI.
        scaled_radius = int(CLICK_MARKER_RADIUS * scale_x)
        scaled_width = max(1, int(CLICK_MARKER_WIDTH * scale_x))
        # ── Modern ripple marker — 3 layers, works on any background ──
        # Layer 1: white outer ring  → visible on dark backgrounds
        # Layer 2: semi-transparent indigo fill → tints without hiding content
        # Layer 3: white centre dot  → pinpoints the exact click pixel
        #
        # The indigo fill is drawn on a separate RGBA overlay at ~40% opacity
        # then composited onto the screenshot so the background remains visible.

        scaled_gap = max(2, int(CLICK_MARKER_OUTER_GAP * scale_x))
        outer_r = scaled_radius + scaled_gap

        # Convert to RGBA for alpha compositing, then draw opaque layers last.
        img = img.convert("RGBA")

        # Layer 2 (drawn first) — semi-transparent indigo fill
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)

        # Parse the hex fill colour and apply 40% alpha (≈ 102 / 255)
        fill_hex = CLICK_MARKER_FILL.lstrip("#")
        fill_r = int(fill_hex[0:2], 16)
        fill_g = int(fill_hex[2:4], 16)
        fill_b = int(fill_hex[4:6], 16)
        fill_rgba = (fill_r, fill_g, fill_b, 102)

        overlay_draw.ellipse(
            [local_x - scaled_radius, local_y - scaled_radius,
             local_x + scaled_radius, local_y + scaled_radius],
            fill=fill_rgba,
        )
        img = Image.alpha_composite(img, overlay)

        # Draw opaque layers on top of the composited image.
        draw = ImageDraw.Draw(img)

        # Layer 1 — white outer ring
        draw.ellipse(
            [local_x - outer_r, local_y - outer_r,
             local_x + outer_r, local_y + outer_r],
            outline=CLICK_MARKER_OUTER,
            width=scaled_width,
        )

        # Indigo outline ring on the filled circle for definition
        draw.ellipse(
            [local_x - scaled_radius, local_y - scaled_radius,
             local_x + scaled_radius, local_y + scaled_radius],
            outline=CLICK_MARKER_FILL,
            width=max(1, scaled_width - 1),
        )

        # Layer 3 — white centre dot (exact click point)
        dot_r = max(3, int(5 * scale_x))
        draw.ellipse(
            [local_x - dot_r, local_y - dot_r,
             local_x + dot_r, local_y + dot_r],
            fill=CLICK_MARKER_CENTER,
        )

        # Convert back to RGB for PNG save (no alpha channel in output).
        img = img.convert("RGB")

        logger.debug(
            "Click marker drawn at physical (%d, %d) from logical (%d, %d) "
            "with scale (%.1f×, %.1f×)",
            local_x, local_y, click_x, click_y, scale_x, scale_y,
        )

        # Save
        filename = f"click_{self._click_counter:04d}.png"
        filepath = self._session_dir / filename
        img.save(str(filepath), "PNG", optimize=True)
        return filepath
