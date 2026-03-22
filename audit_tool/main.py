"""
Main entry point and Tkinter GUI for VibeCheck.

Purpose:
    Provides a modern dark-themed desktop control panel that orchestrates
    all recording subsystems (audio, mouse tracker) and post-processing
    (transcription, report generation).  Users click monitor thumbnails to
    select/switch the capture target, use Start/Pause/Resume/Stop/Cancel
    controls, and receive a structured feedback document on completion.

Inputs:
    User interaction via the GUI (button clicks, monitor selection).

Outputs:
    A session directory under ``~/Downloads/vibecheck-output/<slug>/``
    containing audio, annotated screenshots, and feedback reports (HTML,
    DOCX, MD).

Side Effects:
    - Opens the system microphone.
    - Installs a global mouse listener.
    - Captures screenshots of the selected monitor.
    - Makes an HTTP call to OpenRouter (if API key configured).
    - Opens ``feedback.html`` in the default browser on completion.

Error Behaviour:
    - Displays error dialogs for permission / device failures.
    - Falls back to template report on API failure.

Determinism: Nondeterministic (user-driven).
Idempotency: No — each session is unique.
Thread Safety: GUI runs on the main thread; recording + processing on
    background threads to keep the UI responsive.
Concurrency: Uses ``threading.Thread`` for blocking operations.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import gc
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from pathlib import Path
from typing import Optional, Callable

import mss
from PIL import Image, ImageTk

from audit_tool.audio_recorder import AudioRecorder
from audit_tool.config import create_session_dir, _get_api_key, ProcessMode, JIRA_CONFIG
from audit_tool.mouse_tracker import ClickRecord, MouseTracker
from audit_tool.report_generator import generate_report, cleanup_session, ReportResult, push_to_jira
from audit_tool.transcriber import transcribe, WHISPER_MODELS, WHISPER_MODEL_SIZE

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Theme constants — modern dark palette
# ---------------------------------------------------------------------------
BG = "#0f0f14"
BG_CARD = "#1a1a24"
BG_CARD_HOVER = "#222233"
BG_CARD_SELECTED = "#1e2d4a"
BORDER = "#2a2a3a"
BORDER_SELECTED = "#4ecca3"
FG = "#e8e8ef"
FG_DIM = "#6b6b80"
ACCENT = "#4ecca3"
RED = "#e94560"
ORANGE = "#ff9f43"
FONT = "Helvetica"

THUMB_WIDTH = 160
THUMB_HEIGHT = 100


# ---------------------------------------------------------------------------
# Custom Button widget — macOS Aqua ignores tk.Button bg/fg, so we use
# Frame + Label to get full colour control.
# ---------------------------------------------------------------------------


class StyledButton(tk.Frame):
    """A custom button widget built from Frame + Label.

    macOS Tkinter's Aqua theme overrides ``tk.Button`` colours, making them
    unreadable.  This widget gives full control over background, foreground,
    and hover states on all platforms.

    Attributes:
        _label: The inner Label that displays the text.
        _command: Callback function invoked on click.
        _enabled: Whether the button responds to clicks.
        _bg: Current background colour.
        _fg: Current foreground colour.
        _hover_bg: Hover-state background.
    """

    def __init__(
        self,
        parent: tk.Widget,
        text: str,
        bg: str,
        fg: str,
        command: Optional[Callable] = None,
        font_size: int = 12,
    ) -> None:
        self._bg = bg
        self._fg = fg
        self._hover_bg = self._lighten(bg, 20)
        self._command = command
        self._enabled = True

        super().__init__(
            parent,
            bg=bg,
            cursor="hand2",
            padx=0,
            pady=0,
            highlightbackground="#3a3a50",
            highlightthickness=1,
        )

        self._label = tk.Label(
            self,
            text=text,
            font=(FONT, font_size, "bold"),
            fg=fg,
            bg=bg,
            cursor="hand2",
            pady=12,
            padx=14,
        )
        self._label.pack(fill="both", expand=True)

        # Bind click + hover to both Frame and Label
        for widget in (self, self._label):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

    def _on_click(self, _event: object = None) -> None:
        """Handle click event."""
        if self._enabled and self._command:
            self._command()

    def _on_enter(self, _event: object = None) -> None:
        """Handle mouse enter — hover effect."""
        if self._enabled:
            self.configure(bg=self._hover_bg)
            self._label.configure(bg=self._hover_bg)

    def _on_leave(self, _event: object = None) -> None:
        """Handle mouse leave — reset colours."""
        if self._enabled:
            self.configure(bg=self._bg)
            self._label.configure(bg=self._bg)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the button.

        Args:
            enabled: True to enable, False to disable.

        Side Effects:
            Changes opacity (colour) and cursor.
        """
        self._enabled = enabled
        if enabled:
            self.configure(bg=self._bg, cursor="hand2")
            self._label.configure(fg=self._fg, bg=self._bg, cursor="hand2")
        else:
            dimmed_bg = "#28283a"
            dimmed_fg = "#7a7a90"
            self.configure(bg=dimmed_bg, cursor="arrow", highlightbackground="#333345")
            self._label.configure(fg=dimmed_fg, bg=dimmed_bg, cursor="arrow")

    def set_text(self, text: str) -> None:
        """Update button text."""
        self._label.configure(text=text)

    def set_colors(self, bg: str, fg: str) -> None:
        """Update button colours dynamically.

        Args:
            bg: New background colour.
            fg: New foreground colour.
        """
        self._bg = bg
        self._fg = fg
        self._hover_bg = self._lighten(bg, 20)
        self.configure(bg=bg)
        self._label.configure(bg=bg, fg=fg)

    @staticmethod
    def _lighten(hex_color: str, amount: int) -> str:
        """Lighten a hex colour by a fixed amount.

        Args:
            hex_color: Hex colour string like '#1a6b4f'.
            amount: RGB offset to add (clamped to 255).

        Returns:
            Lightened hex colour string.
        """
        hex_color = hex_color.lstrip("#")
        r = min(int(hex_color[0:2], 16) + amount, 255)
        g = min(int(hex_color[2:4], 16) + amount, 255)
        b = min(int(hex_color[4:6], 16) + amount, 255)
        return f"#{r:02x}{g:02x}{b:02x}"


def _all_children(widget: tk.Widget) -> list[tk.Widget]:
    """Recursively collect all descendant widgets of a Tkinter widget.

    Purpose:
        Used to bind click events to every child widget inside a compound
        card frame so the click is detected regardless of which sub-element
        the user actually hits.

    Args:
        widget: The root widget whose descendants should be collected.

    Returns:
        Flat list of all descendant ``tk.Widget`` instances (depth-first).

    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Must be called from the main Tkinter thread.
    """
    children: list[tk.Widget] = []
    for child in widget.winfo_children():
        children.append(child)
        children.extend(_all_children(child))
    return children


class _PipelineStatusHandler(logging.Handler):
    """Logging handler that forwards pipeline INFO messages to the UI status bar.

    Purpose:
        Bridges `audit_tool.pipeline` log output to the Tkinter status label
        without coupling pipeline.py to the GUI layer.  Mounted and dismounted
        on the pipeline logger per-session in `_process_session`.

    Attributes:
        _callback: Callable that accepts a string and updates the status label.

    Inputs:
        record (logging.LogRecord): Standard Python log record.

    Side Effects:
        Calls `_callback` from the background processing thread; callers must
        ensure the callback is thread-safe (``_set_status_threadsafe`` is).

    Thread Safety: Yes — handler emit() may be called from any thread.
    """

    # Pipeline stage keywords → friendly UI labels
    _STAGE_LABELS: dict[str, str] = {
        "Step 1":  "🔍  Extracting issues from transcript…",
        "Step 2a": "🕐  Filtering screenshots by timestamp…",
        "Step 2b": "🖼   Selecting best screenshot…",
        "Step 3":  "📝  Assembling report…",
    }

    def __init__(self, callback: "Callable[[str], None]") -> None:
        super().__init__(level=logging.INFO)
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        """Forward recognised pipeline stage messages to the UI status bar.

        Args:
            record: The log record emitted by the pipeline logger.

        Side Effects:
            Calls ``self._callback`` if the message matches a known stage.
        """
        message = record.getMessage()
        for keyword, label in self._STAGE_LABELS.items():
            if keyword in message:
                try:
                    self._callback(label)
                except Exception:
                    pass
                return


class AuditRecorderApp:
    """Main application class — Tkinter GUI controller.

    Orchestrates recording lifecycle:
    1. User clicks a monitor thumbnail to select capture target.
    2. Clicks Start → audio recorder + mouse tracker run concurrently.
    3. Pause/Resume as needed.  Switch monitors mid-session.
    4. Stop → transcription → report generation → open report.
    5. Cancel → discard session data.
    """

    def __init__(self) -> None:
        self._root = tk.Tk()
        self._root.title("VibeCheck")
        self._root.configure(bg=BG)
        self._root.resizable(False, False)

        # State
        self._audio_recorder = AudioRecorder()
        self._mouse_tracker = MouseTracker()
        self._session_dir: Optional[Path] = None
        self._recording = False
        self._paused = False
        self._start_time: Optional[float] = None
        self._pause_elapsed: float = 0.0
        self._pause_start: Optional[float] = None
        self._timer_id: Optional[str] = None
        self._thumb_refresh_id: Optional[str] = None
        self._selected_monitor: int = 1
        self._process_mode: ProcessMode = ProcessMode.QA

        # Monitor thumbnails
        self._monitor_photos: list[ImageTk.PhotoImage] = []
        self._monitor_frames: list[tk.Frame] = []

        # Detect monitors
        with mss.mss() as sct:
            self._monitors = sct.monitors

        self._build_ui()
        self._center_window()

        # Start periodic thumbnail refresh so the preview stays live
        self._schedule_thumbnail_refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Construct the application interface."""
        root = self._root

        # ── Title bar ──
        title_frame = tk.Frame(root, bg=BG)
        title_frame.pack(fill="x", padx=24, pady=(18, 0))

        tk.Label(
            title_frame,
            text="🎯  VibeCheck",
            font=(FONT, 18, "bold"),
            fg=FG,
            bg=BG,
        ).pack(side="left")

        # API status pill
        api_text = "● AI Ready" if _get_api_key() else "○ Template Mode"
        api_color = ACCENT if _get_api_key() else FG_DIM
        tk.Label(
            title_frame,
            text=api_text,
            font=(FONT, 10),
            fg=api_color,
            bg=BG,
        ).pack(side="right")

        # ── Separator ──
        sep = tk.Frame(root, bg=BORDER, height=1)
        sep.pack(fill="x", padx=24, pady=(14, 10))

        # ── Process Mode selector ──
        tk.Label(
            root,
            text="PROCESS MODE",
            font=(FONT, 9, "bold"),
            fg=FG_DIM,
            bg=BG,
        ).pack(anchor="w", padx=26, pady=(0, 6))

        mode_frame = tk.Frame(root, bg=BG)
        mode_frame.pack(fill="x", padx=24, pady=(0, 12))

        self._mode_buttons: dict[ProcessMode, tk.Frame] = {}
        mode_definitions = [
            (ProcessMode.QA, "QA Review", "🔍", "Bug / task list for AI agents & Jira"),
            (ProcessMode.DOCUMENTATION, "Documentation", "📖", "SOP / how-to tutorial guide"),
        ]
        for mode_value, mode_label, mode_emoji, mode_desc in mode_definitions:
            card = self._create_mode_card(
                mode_frame, mode_value, mode_emoji, mode_label, mode_desc
            )
            card.pack(side="left", padx=(0, 8), fill="both", expand=True)
            self._mode_buttons[mode_value] = card

        # Auto-select QA mode
        self._select_mode(ProcessMode.QA)

        # ── Monitor label ──
        tk.Label(
            root,
            text="SELECT MONITOR",
            font=(FONT, 9, "bold"),
            fg=FG_DIM,
            bg=BG,
        ).pack(anchor="w", padx=26, pady=(0, 6))

        # ── Monitor thumbnails ──
        monitors_outer = tk.Frame(root, bg=BG)
        monitors_outer.pack(fill="x", padx=24, pady=(0, 14))

        self._monitor_photos = []
        self._monitor_frames = []

        for i in range(1, len(self._monitors)):
            card = self._create_monitor_card(monitors_outer, i)
            card.pack(side="left", padx=(0, 8), pady=0)
            self._monitor_frames.append(card)

        # Auto-select first monitor
        if self._monitor_frames:
            self._select_monitor(1)

        # ── Control buttons — two rows ──
        row1 = tk.Frame(root, bg=BG)
        row1.pack(fill="x", padx=24, pady=(0, 4))

        self._start_btn = StyledButton(
            row1, "▶   Start Recording", "#22906a", "#ffffff", self._on_start
        )
        self._start_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))

        self._pause_btn = StyledButton(
            row1, "⏸   Pause", "#b8872a", "#ffffff", self._on_pause
        )
        self._pause_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))
        self._pause_btn.set_enabled(False)

        row2 = tk.Frame(root, bg=BG)
        row2.pack(fill="x", padx=24, pady=(0, 12))

        self._stop_btn = StyledButton(
            row2, "⏹   Stop & Generate", "#c93050", "#ffffff", self._on_stop
        )
        self._stop_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self._stop_btn.set_enabled(False)

        self._cancel_btn = StyledButton(
            row2, "✕   Cancel", "#3a3a4e", "#cccccc", self._on_cancel, font_size=11
        )
        self._cancel_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))
        self._cancel_btn.set_enabled(False)

        # ── Status bar ──
        # IMPORTANT: In Tkinter, side='right' widgets MUST be packed before
        # side='left' fill widgets, otherwise the fill widget consumes all
        # remaining space and the right-side widgets are clipped.
        status_frame = tk.Frame(root, bg="#08080c")
        status_frame.pack(fill="x", side="bottom", ipady=8)

        # Pack right-side widgets first
        self._timer_label = tk.Label(
            status_frame,
            text="",
            font=("Courier", 13, "bold"),
            fg=ACCENT,
            bg="#08080c",
            anchor="e",
            width=6,   # fixed width prevents clipping (format: MM:SS)
        )
        self._timer_label.pack(side="right", padx=(0, 20))

        self._click_count_label = tk.Label(
            status_frame,
            text="",
            font=(FONT, 10),
            fg=FG_DIM,
            bg="#08080c",
            width=12,  # fixed width: "📸 NNN clicks"
        )
        self._click_count_label.pack(side="right", padx=(0, 4))

        # Status label fills remaining space on the left
        self._status_label = tk.Label(
            status_frame,
            text="Ready — select a monitor and press Start",
            font=(FONT, 10),
            fg=FG_DIM,
            bg="#08080c",
            anchor="w",
        )
        self._status_label.pack(side="left", padx=20, fill="x", expand=True)

    # ------------------------------------------------------------------
    # Mode selector cards
    # ------------------------------------------------------------------

    def _create_mode_card(
        self,
        parent: tk.Frame,
        mode: ProcessMode,
        emoji: str,
        label: str,
        description: str,
    ) -> tk.Frame:
        """Build a clickable process-mode selection card.

        Args:
            parent: Parent frame.
            mode: The ``ProcessMode`` value this card represents.
            emoji: Emoji icon shown on the card.
            label: Short display name.
            description: One-line description shown beneath the label.

        Returns:
            The card Frame widget.
        """
        card = tk.Frame(
            parent,
            bg=BG_CARD,
            highlightbackground=BORDER,
            highlightthickness=2,
            cursor="hand2",
            padx=12,
            pady=10,
        )

        header = tk.Frame(card, bg=BG_CARD, cursor="hand2")
        header.pack(fill="x")

        tk.Label(
            header,
            text=emoji,
            font=(FONT, 16),
            bg=BG_CARD,
            cursor="hand2",
        ).pack(side="left", padx=(0, 6))

        tk.Label(
            header,
            text=label,
            font=(FONT, 11, "bold"),
            fg=FG,
            bg=BG_CARD,
            cursor="hand2",
        ).pack(side="left")

        tk.Label(
            card,
            text=description,
            font=(FONT, 9),
            fg=FG_DIM,
            bg=BG_CARD,
            cursor="hand2",
            wraplength=160,
            justify="left",
        ).pack(fill="x", pady=(4, 0))

        # Bind click to card and all its children
        for widget in _all_children(card):
            widget.bind("<Button-1>", lambda e, m=mode: self._select_mode(m))
        card.bind("<Button-1>", lambda e, m=mode: self._select_mode(m))

        return card

    def _select_mode(self, mode: ProcessMode) -> None:
        """Highlight the selected mode card and update internal state.

        Args:
            mode: The ``ProcessMode`` to activate.

        Side Effects:
            Updates card border highlights and background colours.
            Sets ``self._process_mode``.
        """
        self._process_mode = mode

        for card_mode, card in self._mode_buttons.items():
            selected = card_mode == mode
            border_color = BORDER_SELECTED if selected else BORDER
            bg_color = BG_CARD_SELECTED if selected else BG_CARD
            card.configure(
                highlightbackground=border_color,
                highlightthickness=2,
                bg=bg_color,
            )
            for child in _all_children(card):
                try:
                    child.configure(bg=bg_color)
                except tk.TclError:
                    pass  # Some widgets don't support bg re-config

    # ------------------------------------------------------------------
    # Monitor cards
    # ------------------------------------------------------------------

    def _create_monitor_card(self, parent: tk.Frame, monitor_index: int) -> tk.Frame:
        """Build a clickable monitor thumbnail card.

        Args:
            parent: Parent frame.
            monitor_index: 1-based mss monitor index.

        Returns:
            The card Frame widget.
        """
        mon = self._monitors[monitor_index]
        card = tk.Frame(
            parent,
            bg=BG_CARD,
            highlightbackground=BORDER,
            highlightthickness=2,
            cursor="hand2",
        )

        # Capture thumbnail
        try:
            with mss.mss() as sct:
                raw = sct.grab(mon)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                img.thumbnail((THUMB_WIDTH, THUMB_HEIGHT), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img)
                self._monitor_photos.append(photo)
        except Exception:
            photo = None

        # Thumbnail image
        img_label = tk.Label(card, image=photo, bg=BG_CARD, cursor="hand2")
        img_label.pack(padx=3, pady=(3, 0))

        # Caption
        caption = f"Monitor {monitor_index}  •  {mon['width']}×{mon['height']}"
        cap_label = tk.Label(
            card,
            text=caption,
            font=(FONT, 8),
            fg=FG_DIM,
            bg=BG_CARD,
            cursor="hand2",
        )
        cap_label.pack(pady=(2, 4))

        # Bind click events on all children
        for widget in (card, img_label, cap_label):
            widget.bind("<Button-1>", lambda e, idx=monitor_index: self._select_monitor(idx))

        return card

    def _select_monitor(self, monitor_index: int) -> None:
        """Highlight the selected monitor card and update state.

        Args:
            monitor_index: 1-based monitor index.

        Side Effects:
            Updates border highlight on cards.
            If recording, switches the tracker to the new monitor.
        """
        self._selected_monitor = monitor_index

        for i, card in enumerate(self._monitor_frames):
            idx = i + 1
            if idx == monitor_index:
                card.configure(highlightbackground=BORDER_SELECTED, highlightthickness=2)
                for child in card.winfo_children():
                    child.configure(bg=BG_CARD_SELECTED)
                card.configure(bg=BG_CARD_SELECTED)
            else:
                card.configure(highlightbackground=BORDER, highlightthickness=2)
                for child in card.winfo_children():
                    child.configure(bg=BG_CARD)
                card.configure(bg=BG_CARD)

        if self._recording and self._mouse_tracker.is_tracking:
            self._mouse_tracker.switch_monitor(monitor_index)
            self._set_status(f"🔀  Switched to Monitor {monitor_index}")

    def _refresh_thumbnails(self) -> None:
        """Refresh all monitor thumbnail images."""
        for i, card in enumerate(self._monitor_frames):
            monitor_index = i + 1
            mon = self._monitors[monitor_index]
            try:
                with mss.mss() as sct:
                    raw = sct.grab(mon)
                    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                    img.thumbnail((THUMB_WIDTH, THUMB_HEIGHT), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(img)
                    self._monitor_photos[i] = photo
                    children = card.winfo_children()
                    if children:
                        children[0].configure(image=photo)
            except Exception:
                pass

    _THUMB_REFRESH_INTERVAL_MS: int = 3000  # 3 seconds

    def _schedule_thumbnail_refresh(self) -> None:
        """Periodically refresh monitor thumbnails so the preview stays live.

        Captures a new screenshot of each monitor every 3 seconds and updates
        the thumbnail widgets.  This keeps the previews current as the user
        switches windows on the monitored screen.

        Side Effects:
            Schedules a recurring ``after`` callback on the Tkinter main loop.
            Captures screenshots via ``mss``.

        Determinism: Deterministic scheduling; screenshot content varies.
        Idempotency: Safe to call multiple times (cancels previous timer).
        Thread Safety: Must be called from the main thread.
        """
        # Cancel any existing scheduled refresh
        if self._thumb_refresh_id is not None:
            self._root.after_cancel(self._thumb_refresh_id)
            self._thumb_refresh_id = None

        self._refresh_thumbnails()
        self._thumb_refresh_id = self._root.after(
            self._THUMB_REFRESH_INTERVAL_MS,
            self._schedule_thumbnail_refresh,
        )

    # ------------------------------------------------------------------
    # Window geometry
    # ------------------------------------------------------------------

    def _center_window(self) -> None:
        """Size the window to fit all content, then centre it on the primary display.

        Uses ``winfo_reqheight()`` after ``update_idletasks()`` to measure the
        true required height so the window auto-expands when new UI sections
        (e.g. the mode selector) are added, rather than relying on a
        hardcoded constant that clips content.

        Side Effects:
            Calls ``self._root.geometry()`` to resize and reposition the window.
        """
        self._root.update_idletasks()
        num_monitors = len(self._monitors) - 1
        card_width = THUMB_WIDTH + 14
        content_width = max(num_monitors * card_width + 60, 540)
        width = min(content_width, 960)

        # Let Tkinter measure the actual required height rather than guessing.
        # Add a small buffer (16 px) to prevent Tkinter from clipping the
        # status bar's bottom border on some macOS window managers.
        required_height = self._root.winfo_reqheight()
        height = max(required_height + 16, 520)  # floor at 520 to look intentional

        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        self._root.geometry(f"{width}x{height}+{x}+{y}")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        """Handle Start button click."""
        if self._recording:
            return

        self._session_dir = create_session_dir()
        logger.info("Session directory: %s", self._session_dir)

        try:
            self._audio_recorder.start(self._session_dir)
        except Exception as audio_error:
            messagebox.showerror(
                "Microphone Error",
                f"Could not start audio recording:\n{audio_error}\n\n"
                "Check System Settings → Privacy & Security → Microphone.",
            )
            return

        try:
            self._mouse_tracker.start(self._session_dir, self._selected_monitor)
        except Exception as tracker_error:
            self._audio_recorder.stop()
            messagebox.showerror(
                "Accessibility Error",
                f"Could not start mouse tracker:\n{tracker_error}\n\n"
                "Check System Settings → Privacy & Security → Accessibility.",
            )
            return

        self._recording = True
        self._paused = False
        self._start_time = time.time()
        self._pause_elapsed = 0.0
        self._pause_start = None

        self._start_btn.set_enabled(False)
        self._pause_btn.set_enabled(True)
        self._stop_btn.set_enabled(True)
        self._cancel_btn.set_enabled(True)
        self._set_status("🔴  Recording — speak and click to capture findings")
        self._update_timer()

    def _on_pause(self) -> None:
        """Handle Pause / Resume toggle."""
        if not self._recording:
            return

        if self._paused:
            self._audio_recorder.resume()
            self._mouse_tracker.resume()
            self._paused = False
            if self._pause_start is not None:
                self._pause_elapsed += time.time() - self._pause_start
                self._pause_start = None
            self._pause_btn.set_text("⏸   Pause")
            self._pause_btn.set_colors("#b8872a", "#ffffff")
            self._set_status("🔴  Recording resumed")
            self._update_timer()
        else:
            self._audio_recorder.pause()
            self._mouse_tracker.pause()
            self._paused = True
            self._pause_start = time.time()
            if self._timer_id:
                self._root.after_cancel(self._timer_id)
                self._timer_id = None
            self._pause_btn.set_text("▶   Resume")
            self._pause_btn.set_colors("#22906a", "#ffffff")
            self._set_status("⏸  Paused — click Resume to continue")

    def _on_stop(self) -> None:
        """Handle Stop & Generate button — show model picker, then process."""
        if not self._recording:
            return

        selected_model = self._show_model_picker()
        if selected_model is None:
            return  # User cancelled the dialog

        self._recording = False
        self._paused = False
        if self._timer_id:
            self._root.after_cancel(self._timer_id)
            self._timer_id = None

        self._stop_btn.set_enabled(False)
        self._pause_btn.set_enabled(False)
        self._cancel_btn.set_enabled(False)

        # Pause thumbnail refresh during processing to free CPU
        if self._thumb_refresh_id is not None:
            self._root.after_cancel(self._thumb_refresh_id)
            self._thumb_refresh_id = None

        self._set_status(f"⏳  Stopping recorders (model: {selected_model})…")
        self._root.update_idletasks()

        thread = threading.Thread(
            target=self._process_session,
            args=(selected_model,),
            daemon=True,
        )
        thread.start()

    def _show_model_picker(self) -> str | None:
        """Show a dialog for selecting the Whisper transcription model.

        Returns:
            The selected model name (e.g. 'medium'), or None if cancelled.

        Side Effects:
            Creates a modal Toplevel dialog.

        Determinism: Deterministic.
        Idempotency: Yes.
        Thread Safety: Must be called from the main thread.
        """
        dialog = tk.Toplevel(self._root)
        dialog.title("Transcription Settings")
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        # NOTE: dialog.transient() intentionally omitted — on macOS, transient
        # Toplevels vanish when dragged to a different screen because the WM
        # ties their visibility to the parent's screen. Keeping grab_set()
        # alone preserves modality without the visibility bug.
        dialog.grab_set()

        # Center on parent
        dialog.update_idletasks()
        parent_x = self._root.winfo_rootx()
        parent_y = self._root.winfo_rooty()
        parent_w = self._root.winfo_width()
        dialog.geometry(f"+{parent_x + parent_w // 2 - 180}+{parent_y + 60}")

        # Ensure dialog is visible and focused on any screen
        dialog.lift()
        dialog.focus_force()

        # Title
        tk.Label(
            dialog,
            text="WHISPER MODEL",
            font=(FONT, 10, "bold"),
            fg=FG_DIM,
            bg=BG,
        ).pack(anchor="w", padx=20, pady=(16, 4))

        tk.Label(
            dialog,
            text="Larger models are slower but better\nfor mixed-language sessions.",
            font=(FONT, 10),
            fg="#888",
            bg=BG,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 10))

        selected = tk.StringVar(value=WHISPER_MODEL_SIZE)
        result: list[str | None] = [None]

        for model_key, description in WHISPER_MODELS.items():
            frame = tk.Frame(dialog, bg=BG)
            frame.pack(fill="x", padx=20, pady=2)

            rb = tk.Radiobutton(
                frame,
                text=f"  {model_key}",
                variable=selected,
                value=model_key,
                font=(FONT, 12, "bold"),
                fg="#e0e0e0",
                bg=BG,
                selectcolor="#1a1a24",
                activebackground=BG,
                activeforeground="#4ecca3",
                highlightthickness=0,
                anchor="w",
            )
            rb.pack(side="left")

            tk.Label(
                frame,
                text=f"  {description}",
                font=(FONT, 10),
                fg="#777",
                bg=BG,
                anchor="w",
            ).pack(side="left", fill="x")

        # Buttons row
        btn_frame = tk.Frame(dialog, bg=BG)
        btn_frame.pack(fill="x", padx=20, pady=(14, 16))

        def on_process():
            result[0] = selected.get()
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)

        process_btn = StyledButton(
            btn_frame, "▶  Process", "#22906a", "#ffffff", on_process
        )
        process_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))

        cancel_btn = StyledButton(
            btn_frame, "Cancel", "#3a3a4e", "#cccccc", on_cancel, font_size=11
        )
        cancel_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        dialog.wait_window()
        return result[0]

    def _on_cancel(self) -> None:
        """Handle Cancel button — discard the current session."""
        if not self._recording:
            return

        confirm = messagebox.askyesno(
            "Cancel Recording",
            "Discard the current session?\nAll audio and screenshots will be deleted.",
        )
        if not confirm:
            return

        self._recording = False
        self._paused = False
        if self._timer_id:
            self._root.after_cancel(self._timer_id)
            self._timer_id = None

        try:
            self._audio_recorder.stop()
        except Exception:
            pass
        try:
            self._mouse_tracker.stop()
        except Exception:
            pass

        if self._session_dir and self._session_dir.exists():
            shutil.rmtree(self._session_dir, ignore_errors=True)
            logger.info("Session cancelled, deleted: %s", self._session_dir)

        self._set_status("✕  Session cancelled")
        self._reset_ui()

    # ------------------------------------------------------------------
    # Background processing
    # ------------------------------------------------------------------

    def _process_session(self, model_size: str) -> None:
        """Background thread: stop recorders → transcribe → generate report.

        Args:
            model_size: Whisper model to use for transcription.
        """
        try:
            wav_path = self._audio_recorder.stop()
            clicks = self._mouse_tracker.stop()
            self._set_status_threadsafe(
                f"⏳  Transcribing audio ({wav_path.name}, model: {model_size})…"
            )

            segments = transcribe(wav_path, model_size=model_size)
            self._set_status_threadsafe(
                f"⏳  Generating report ({len(segments)} segments, "
                f"{len(clicks)} clicks, mode: {self._process_mode.value})…"
            )

            # Wire per-step progress updates into the UI status bar.
            # The callback is called by the pipeline at each stage transition.
            def pipeline_status_callback(message: str) -> None:
                self._set_status_threadsafe(message)

            # Patch the pipeline logger to forward INFO-level step messages
            # to the UI status bar without requiring a pipeline API change.
            import logging as _logging
            _pipeline_handler = _PipelineStatusHandler(pipeline_status_callback)
            _pipeline_logger = _logging.getLogger("audit_tool.pipeline")
            _pipeline_logger.addHandler(_pipeline_handler)

            try:
                assert self._session_dir is not None
                result = generate_report(
                    self._session_dir, segments, clicks, mode=self._process_mode
                )
            finally:
                _pipeline_logger.removeHandler(_pipeline_handler)

            # Take a snapshot of clicks before we delete them — needed for
            # the on-demand Jira push button in _show_completion_dialog.
            clicks_snapshot = list(clicks)

            # Release large objects now that reports are written
            del segments, clicks
            gc.collect()

            self._set_status_threadsafe("⏳  Cleaning up session files…")
            final_dir = cleanup_session(self._session_dir, result)

            # The report_path was set before the dir rename — fix it
            final_report = final_dir / result.report_path.name

            cost_str = f"  •  AI cost: {result.cost_display}" if result.cost_usd else ""
            jira_str = (
                f"  •  Jira: {', '.join(result.jira_keys)}"
                if result.jira_keys
                else ""
            )
            self._set_status_threadsafe(
                f"✅  Report saved: {final_report.name}{cost_str}{jira_str}"
            )
            self._root.after(0, lambda p=final_report: self._open_file(p))

        except Exception as processing_error:
            logger.exception("Session processing failed")
            self._root.after(
                0,
                lambda: messagebox.showerror(
                    "Processing Error",
                    f"Failed to process session:\n{processing_error}",
                ),
            )
            self._set_status_threadsafe("❌  Processing failed — see logs")

        finally:
            self._session_dir = None
            gc.collect()
            self._root.after(0, self._reset_ui)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        """Update status bar text (call from main thread only)."""
        self._status_label.configure(text=text)

    def _set_status_threadsafe(self, text: str) -> None:
        """Update status bar text from any thread."""
        self._root.after(0, lambda: self._set_status(text))

    def _update_timer(self) -> None:
        """Update the elapsed-time and click-count labels."""
        if not self._recording or self._paused or self._start_time is None:
            return
        elapsed = int(time.time() - self._start_time - self._pause_elapsed)
        minutes = elapsed // 60
        seconds = elapsed % 60
        self._timer_label.configure(text=f"{minutes:02d}:{seconds:02d}")
        self._click_count_label.configure(
            text=f"📸 {self._mouse_tracker.click_count} clicks"
        )
        self._timer_id = self._root.after(1000, self._update_timer)

    def _reset_ui(self) -> None:
        """Reset buttons back to the initial state."""
        self._start_btn.set_enabled(True)
        self._pause_btn.set_enabled(False)
        self._pause_btn.set_text("⏸   Pause")
        self._pause_btn.set_colors("#b8872a", "#ffffff")
        self._stop_btn.set_enabled(False)
        self._cancel_btn.set_enabled(False)
        self._timer_label.configure(text="")
        self._click_count_label.configure(text="")

        # Restart periodic thumbnail refresh (paused during processing)
        self._schedule_thumbnail_refresh()

    def _show_completion_dialog(
        self,
        result: ReportResult,
        report_path: Path,
        clicks: list,
    ) -> None:
        """Show the post-processing completion dialog.

        Displays the report filename, AI cost, any auto-pushed Jira keys,
        and — when Jira is configured — a manual "Push to Jira" button.
        Also opens the HTML report automatically.

        Args:
            result: The ``ReportResult`` from ``generate_report``.
            report_path: Resolved final path to the HTML report.
            clicks: Click records snapshot (needed for Jira attachment lookup).

        Side Effects:
            Opens the report file. Creates a modal ``Toplevel`` dialog.
        """
        # Open report in browser immediately
        self._open_file(report_path)

        dialog = tk.Toplevel(self._root)
        dialog.title("✅ Report Complete")
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.transient(self._root)

        # Centre on parent
        dialog.update_idletasks()
        parent_x = self._root.winfo_rootx()
        parent_y = self._root.winfo_rooty()
        parent_w = self._root.winfo_width()
        dialog.geometry(f"+{parent_x + parent_w // 2 - 220}+{parent_y + 80}")

        # ── Header ──
        tk.Label(
            dialog,
            text="✅  Report Ready",
            font=(FONT, 14, "bold"),
            fg="#4ecca3",
            bg=BG,
        ).pack(anchor="w", padx=24, pady=(20, 4))

        tk.Label(
            dialog,
            text=report_path.name,
            font=(FONT, 10),
            fg=FG_DIM,
            bg=BG,
            anchor="w",
        ).pack(anchor="w", padx=24)

        # ── Cost / Jira row ──
        info_frame = tk.Frame(dialog, bg=BG)
        info_frame.pack(fill="x", padx=24, pady=(10, 0))

        if result.cost_usd:
            tk.Label(
                info_frame,
                text=f"💰  {result.cost_display}",
                font=(FONT, 10),
                fg=FG_DIM,
                bg=BG,
            ).pack(anchor="w")

        auto_keys_label = None
        if result.jira_keys:
            auto_keys_label = tk.Label(
                info_frame,
                text=f"🔗  Auto-pushed: {', '.join(result.jira_keys)}",
                font=(FONT, 10),
                fg="#4ecca3",
                bg=BG,
            )
            auto_keys_label.pack(anchor="w", pady=(4, 0))

        # ── Jira push status label (shown during/after manual push) ──
        jira_status_label = tk.Label(
            dialog,
            text="",
            font=(FONT, 10),
            fg=FG_DIM,
            bg=BG,
            wraplength=380,
            justify="left",
        )
        jira_status_label.pack(anchor="w", padx=24, pady=(4, 0))

        # ── Buttons ──
        sep = tk.Frame(dialog, bg=BORDER, height=1)
        sep.pack(fill="x", padx=24, pady=(14, 12))

        btn_frame = tk.Frame(dialog, bg=BG)
        btn_frame.pack(fill="x", padx=24, pady=(0, 20))

        # Push to Jira button — only shown when Jira is configured
        if JIRA_CONFIG is not None and not result.jira_keys:
            jira_btn_ref: list[Optional["StyledButton"]] = [None]

            def _push_to_jira_thread():
                """Background: push tasks to Jira and update dialog."""
                dialog.after(
                    0,
                    lambda: jira_status_label.configure(
                        text="⏳  Pushing to Jira…", fg=FG_DIM
                    ),
                )
                if jira_btn_ref[0]:
                    dialog.after(0, lambda: jira_btn_ref[0].set_enabled(False))
                try:
                    keys = push_to_jira(
                        JIRA_CONFIG, result.markdown_content, clicks, self._process_mode
                    )
                    if keys:
                        msg = f"🔗  Created: {', '.join(keys)}"
                        color = "#4ecca3"
                    else:
                        msg = "⚠️  No issues were created — check logs."
                        color = "#e0a800"
                    dialog.after(
                        0,
                        lambda m=msg, c=color: jira_status_label.configure(
                            text=m, fg=c
                        ),
                    )
                except Exception as jira_error:
                    dialog.after(
                        0,
                        lambda e=str(jira_error): jira_status_label.configure(
                            text=f"❌  Jira error: {e[:120]}", fg="#e05555"
                        ),
                    )

            def _on_jira_push():
                threading.Thread(
                    target=_push_to_jira_thread, daemon=True
                ).start()

            jira_btn = StyledButton(
                btn_frame,
                "🔗  Push to Jira",
                "#1a4d8c",
                "#ffffff",
                _on_jira_push,
            )
            jira_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
            jira_btn_ref[0] = jira_btn

        open_btn = StyledButton(
            btn_frame,
            "📂  Open Report",
            "#22906a",
            "#ffffff",
            lambda: self._open_file(report_path),
        )
        open_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

        close_btn = StyledButton(
            btn_frame, "Close", "#3a3a4e", "#cccccc", dialog.destroy, font_size=11
        )
        close_btn.pack(side="left", expand=True, fill="x")

    @staticmethod
    def _open_file(path: Path) -> None:
        """Open a file with the system default application (macOS)."""

        try:
            subprocess.Popen(["open", str(path)])
        except Exception:
            logger.warning("Could not open file: %s", path)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the Tkinter main loop.

        This is a blocking call — it returns when the window is closed.
        """
        self._root.mainloop()


def main() -> None:
    """Application entry point."""
    app = AuditRecorderApp()
    app.run()


if __name__ == "__main__":
    main()
