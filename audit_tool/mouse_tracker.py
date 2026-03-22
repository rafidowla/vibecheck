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
import queue
import threading
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
        # Coordinate correction factor for pynput in .app bundles.
        # On macOS Retina displays, pynput (CGEvent) may report coordinates
        # in a different scale than mss's logical points.  Detected at start().
        self._pynput_scale_x: float = 1.0
        self._pynput_scale_y: float = 1.0
        # Background worker thread + queue for screenshot capture.
        # The pynput CGEventTap callback MUST return immediately; if it blocks
        # for more than ~5ms macOS auto-disables the event tap, silently
        # dropping all future click events.  We enqueue the raw click data
        # and let the worker thread do the heavy I/O.
        self._click_queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None

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
        # Record the moment tracking starts so we can suppress the
        # "Start Recording" button click itself (warmup window).
        self._tracking_start_epoch: float = time.time()

        # Detect pynput ↔ mss coordinate mismatch on macOS Retina.
        self._detect_pynput_scale()

        # Start the background screenshot worker BEFORE the listener so
        # the queue is being drained as soon as clicks arrive.
        self._click_queue = queue.Queue()
        self._worker_thread = threading.Thread(
            target=self._screenshot_worker,
            name="VibeCheck-ClickWorker",
            daemon=True,
        )
        self._worker_thread.start()

        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()

    def _detect_pynput_scale(self) -> None:
        """Detect coordinate scaling mismatch between pynput and mss.

        On macOS Retina displays, pynput (via CGEvent) may report click
        coordinates in a different scale than mss's logical-point system.
        This is especially common when running inside a PyInstaller .app
        bundle where the coordinate spaces can diverge.

        This method compares the main display bounds as reported by Quartz
        (physical pixels) with the mss monitor dimensions (logical points)
        to compute a correction factor.

        Side Effects:
            Sets ``_pynput_scale_x`` and ``_pynput_scale_y``.

        Determinism: Deterministic for a given display configuration.
        Idempotency: Yes — safe to call multiple times.
        """
        import platform as _platform

        if _platform.system() != "Darwin":
            return

        try:
            import Quartz

            # Quartz reports the main display in logical points
            main_display = Quartz.CGMainDisplayID()
            cg_bounds = Quartz.CGDisplayBounds(main_display)
            cg_width = cg_bounds.size.width
            cg_height = cg_bounds.size.height

            # mss reports the main display (index 1) in logical points
            if self._sct and len(self._sct.monitors) > 1:
                mss_mon = self._sct.monitors[1]  # main display
                mss_width = mss_mon["width"]
                mss_height = mss_mon["height"]

                if cg_width > 0 and cg_height > 0:
                    self._pynput_scale_x = mss_width / cg_width
                    self._pynput_scale_y = mss_height / cg_height

                    if (abs(self._pynput_scale_x - 1.0) > 0.01
                            or abs(self._pynput_scale_y - 1.0) > 0.01):
                        logger.info(
                            "Pynput coordinate correction: scale_x=%.3f, "
                            "scale_y=%.3f (Quartz main=%.0fx%.0f, "
                            "mss main=%dx%d)",
                            self._pynput_scale_x, self._pynput_scale_y,
                            cg_width, cg_height,
                            mss_width, mss_height,
                        )
                    else:
                        logger.debug(
                            "No pynput coordinate correction needed "
                            "(scale factors ≈ 1.0)."
                        )
        except ImportError:
            logger.debug("Quartz not available — skipping pynput scale detection.")
        except Exception:
            logger.exception("Failed to detect pynput coordinate scale.")

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

        # Signal the worker thread to finish and wait for any in-flight
        # screenshots to complete before we close the mss context.
        if self._worker_thread is not None:
            self._click_queue.put(None)   # sentinel: tells worker to exit
            self._worker_thread.join(timeout=10.0)
            self._worker_thread = None

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
        """Callback fired on every global mouse event (CGEventTap).

        CRITICAL — this method MUST do ZERO I/O and return in microseconds.
        Any file open/write/close or syscall here can delay the CGEventTap
        callback long enough for macOS to stop delivering events — silently
        dropping all subsequent clicks for the rest of the session.

        This callback only:
          1. Checks guard conditions (press-only, active, warmup).
          2. Records the timestamp (time.time() is a fast VDSO call).
          3. Pushes a lightweight tuple to ``_click_queue`` (non-blocking).

        ALL I/O — screenshots, Pillow, disk writes, diagnostic logging —
        is done in ``_screenshot_worker`` on a background thread.

        Side Effects:
            Enqueues a tuple for the worker thread.
        """
        if not pressed or not self._active or self._paused:
            return

        click_ts = time.time()
        elapsed = click_ts - self._tracking_start_epoch

        # No I/O here. Pass everything the worker needs via the queue.
        # is_warmup=True items are logged-only (not captured as screenshots).
        is_warmup = elapsed < 3.0
        self._click_queue.put((x, y, click_ts, elapsed, button.name, is_warmup))


    def _screenshot_worker(self) -> None:
        """Background worker: drains ``_click_queue`` and captures screenshots.

        Runs on a dedicated daemon thread so the CGEventTap callback
        can return instantly.  Processes one click at a time to avoid
        concurrent mss/Pillow state.

        Exits when ``None`` is dequeued (sentinel sent by ``stop()``).

        Side Effects:
            Captures screenshots via mss, writes PNGs to disk, appends
            to ``self._clicks``.
        """
        while True:
            item = self._click_queue.get()
            if item is None:  # sentinel from stop()
                break

            x_raw, y_raw, click_ts, elapsed, button_name, is_warmup = item

            # Apply pynput coordinate correction for .app bundles.
            x = x_raw * self._pynput_scale_x
            y = y_raw * self._pynput_scale_y

            # Write the RECEIVED log entry here (worker thread, not callback).
            try:
                _log = Path.home() / ".vibecheck" / "clicks.log"
                _log.parent.mkdir(parents=True, exist_ok=True)
                with _log.open("a", encoding="utf-8") as _f:
                    tag = "WARMUP " if is_warmup else "RECEIVED"
                    _f.write(
                        f"{tag}: btn={button_name} "
                        f"raw=({x_raw:.1f},{y_raw:.1f}) epoch={click_ts:.3f} "
                        f"T+{elapsed:.2f}s\n"
                    )
            except Exception:
                pass

            # Warmup clicks: logged above but not captured as screenshots.
            if is_warmup:
                continue

            if self._sct is None:
                logger.debug("mss context gone — skipping queued click at (%g,%g).", x, y)
                continue

            # Find which monitor the click landed on.
            capture_monitor_index = self._monitor_index
            for idx, mon in enumerate(self._sct.monitors[1:], start=1):
                if (mon["left"] <= x < mon["left"] + mon["width"]
                        and mon["top"] <= y < mon["top"] + mon["height"]):
                    capture_monitor_index = idx
                    break
            else:
                try:
                    _log = Path.home() / ".vibecheck" / "clicks.log"
                    with _log.open("a", encoding="utf-8") as _f:
                        _f.write(
                            f"NOMATCH: ({x:.1f},{y:.1f}) epoch={time.time():.3f} "
                            f"— not on any of {len(self._sct.monitors)-1} monitors\n"
                        )
                except Exception:
                    pass
                logger.warning("Click at (%g,%g) not on any monitor — skipping.", x, y)
                continue

            if capture_monitor_index != self._monitor_index:
                logger.info(
                    "Click at (%g,%g) is on monitor %d — capturing that monitor.",
                    x, y, capture_monitor_index,
                )

            # Brief delay so macOS finishes rendering the click's UI changes.
            time.sleep(0.05)

            try:
                screenshot_path = self._capture_and_annotate(
                    x, y, monitor_index=capture_monitor_index
                )
                record = ClickRecord(
                    index=self._click_counter,
                    timestamp=click_ts,
                    x=int(x),
                    y=int(y),
                    screenshot_path=screenshot_path,
                    monitor_index=capture_monitor_index,
                )
                self._clicks.append(record)
                self._click_counter += 1
            except Exception:
                logger.exception("Worker failed to capture screenshot at (%g,%g).", x, y)




    def _capture_and_annotate(
        self,
        click_x: int,
        click_y: int,
        monitor_index: int | None = None,
    ) -> Path:
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
            monitor_index: ``mss`` monitor index to capture.  If ``None``, uses
                ``self._monitor_index`` (the user-selected default).  Pass an
                explicit value when the click landed on a different monitor than
                the pre-selected one (multi-monitor layouts).

        Returns:
            Path to the saved annotated PNG.

        Side Effects:
            Writes a PNG file to the session directory.
        """
        assert self._sct is not None
        assert self._session_dir is not None

        _mon_idx = monitor_index if monitor_index is not None else self._monitor_index
        monitor = self._sct.monitors[_mon_idx]

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

        # ── Clamp to image bounds ──
        # This is a last-resort guard. By the time we reach _capture_and_annotate
        # the bounds check in _on_click should have already rejected out-of-range
        # clicks. However, floating-point rounding or monitor hotplug edge cases
        # can still produce a local pixel that is slightly outside the canvas.
        local_x = max(0, min(local_x, physical_width - 1))
        local_y = max(0, min(local_y, physical_height - 1))

        # Diagnostic log — written AFTER clamping so the logged values always
        # reflect what will actually be drawn onto the screenshot.
        try:
            import time as _time
            from pathlib import Path as _Path
            _log = _Path.home() / ".vibecheck" / "clicks.log"
            _log.parent.mkdir(parents=True, exist_ok=True)
            _epoch = _time.time()
            with _log.open("a", encoding="utf-8") as _f:
                _f.write(
                    f"Click: raw_pynput=({click_x:.1f},{click_y:.1f}) "
                    f"epoch={_epoch:.3f} "
                    f"mon_idx={self._monitor_index} "
                    f"mon_bounds=(left={monitor['left']},top={monitor['top']},"
                    f"w={monitor['width']},h={monitor['height']}) "
                    f"capture_size={physical_width}x{physical_height} "
                    f"scale=({scale_x:.3f},{scale_y:.3f}) "
                    f"local_pixel=({local_x},{local_y})\n"
                )
        except Exception:
            pass

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
