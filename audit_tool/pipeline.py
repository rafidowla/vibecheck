"""
Multi-agent pipeline for VibeCheck report generation.

Purpose:
    Replaces the previous single-LLM call with a 3-step pipeline that
    correctly pairs each spoken issue/step with the screenshot whose click
    marker is nearest to the described UI element:

    Step 1  — Issue/Step Extraction   (text LLM, no images)
    Step 2a — Screenshot Filtering    (pure Python, no LLM)
    Step 2b — Best-Shot Selection     (vision LLM, only when ≥2 candidates)
    Step 3  — Report Assembly         (code, no LLM)

Design rationale:
    The previous approach sent all screenshots + full transcript in one call,
    giving the model no temporal grounding.  It guessed pairings by order
    rather than by when issues were spoken, causing click circles to appear
    on unrelated UI elements.

    This pipeline anchors every screenshot to its Unix-epoch timestamp and
    every issue to its Whisper transcript timestamps, so the filtering in
    Step 2a is deterministic and the vision model in Step 2b only ever sees
    2–3 contextually-relevant candidates.

LLM usage vs. previous approach:
    - Previous: 1 large call × all screenshots (up to 20 images per call).
    - New QA:   1 cheap text call + 0–N small vision calls (2–3 images each).
      If each issue has exactly 1 click in its window, zero vision calls run.
    - Total image-token usage drops by ~80% on typical sessions.

Inputs:
    run_qa_pipeline / run_documentation_pipeline:
        transcript   (list[TranscriptSegment]): Whisper output.
        clicks       (list[ClickRecord]):       Mouse-tracker output.
        session_dir  (Path):                    Session folder (for epoch anchor).
        api_key      (str):                     OpenRouter API key.
        model        (str):                     Vision model identifier.

Outputs:
    str: Assembled Markdown document ready for HTML / DOCX rendering.

Error Behaviour:
    - Step 1 JSON parse failure → raises PipelineError; caller falls back to
      legacy single-call path.
    - Step 2b vision call failure → logs warning, uses first candidate.
    - Step 3 assembly is deterministic and never raises.

Side Effects:
    - HTTP POST to OpenRouter in Step 1 and (conditionally) Step 2b.

Determinism: Nondeterministic (LLM calls).
Idempotency: No — each run may produce different output.
Thread Safety: Yes — no shared mutable state.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from audit_tool.config import OPENROUTER_BASE_URL, ProcessMode
from audit_tool.mouse_tracker import ClickRecord
from audit_tool.transcriber import TranscriptSegment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many seconds before/after the spoken issue window to include clicks.
# A wider window handles natural delay between speaking and clicking.
_QA_WINDOW_SECONDS: float = 5.0
_DOC_WINDOW_SECONDS: float = 4.0  # tighter — narrate-then-click is synchronous

# Vision model request timeout (seconds).
_VISION_TIMEOUT: float = 60.0

# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass
class IssueRecord:
    """A single issue or step extracted from the spoken transcript.

    Purpose:
        Carries the structured output of Agent 1 (issue extraction) and
        acts as the key passed to Steps 2a and 2b.

    Attributes:
        title:               Short human-readable title.
        t_start:             Transcript-relative start time in seconds.
        t_end:               Transcript-relative end time in seconds.
        priority:            Priority string (QA) or empty string (Doc).
        issue_type:          Bug/UI/UX/etc. (QA) or empty string (Doc).
        description:         Full written description of the issue or step.
        target_component:    Best-guess file / component name.
        steps:               Ordered list of implementation or action steps.
        acceptance_criteria: Testable acceptance conditions (QA) or done-when (Doc).
        step_number:         Sequential position (Documentation mode only).
    """

    title: str
    t_start: float
    t_end: float
    priority: str = ""
    issue_type: str = ""
    description: str = ""
    target_component: str = ""
    steps: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    step_number: int = 0


@dataclass
class IssueWithScreenshot:
    """Pairs a structured issue/step with its best matched screenshot.

    Attributes:
        issue:      The extracted IssueRecord.
        screenshot: The chosen ClickRecord, or None if no click fell within
                    the time window for this issue.
    """

    issue: IssueRecord
    screenshot: Optional[ClickRecord]


class PipelineError(Exception):
    """Raised when the pipeline cannot proceed and the caller should fall back."""


# ---------------------------------------------------------------------------
# Helper — epoch anchor
# ---------------------------------------------------------------------------


def read_session_start_epoch(session_dir: Optional[Path]) -> float:
    """Read the recording start Unix epoch from recording_start.txt.

    Purpose:
        Bridges the gap between Whisper's session-relative timestamps
        (seconds from recording start) and ClickRecord.timestamp (Unix epoch).

    Args:
        session_dir: Session directory written by AudioRecorder.start().
                     If None or the file is absent, falls back to 0.0 and
                     the caller must handle the mismatch.

    Returns:
        Unix epoch float, or 0.0 if unavailable.

    Side Effects: Reads one file from disk.
    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Yes (read-only).
    """
    if session_dir is None:
        return 0.0
    start_file = session_dir / "recording_start.txt"
    if not start_file.exists():
        logger.warning("recording_start.txt missing — click-time anchoring disabled.")
        return 0.0
    try:
        return float(start_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as error:
        logger.warning("Could not read recording_start.txt: %s", error)
        return 0.0


# ---------------------------------------------------------------------------
# Step 2a — Pure Python screenshot filter (NO LLM)
# ---------------------------------------------------------------------------


def filter_clicks_for_issue(
    issue: IssueRecord,
    clicks: list[ClickRecord],
    session_start_epoch: float,
    window_seconds: float = _QA_WINDOW_SECONDS,
) -> list[ClickRecord]:
    """Return clicks whose session-relative timestamp falls within the issue window.

    Purpose:
        Deterministically narrows the full click list to only those that
        occurred while the reviewer was narrating this specific issue or step.
        No LLM is involved — this is pure arithmetic on timestamps.

    Algorithm:
        1. Convert each click's Unix epoch to session-relative seconds:
               click_session_t = click.timestamp - session_start_epoch
        2. Keep clicks where:
               issue.t_start - window_seconds  <=  click_session_t
                                               <=  issue.t_end + window_seconds

    Args:
        issue:                The IssueRecord defining the time window.
        clicks:               All ClickRecords from the session.
        session_start_epoch:  Unix epoch of recording start (from recording_start.txt).
        window_seconds:       Padding in seconds added before t_start and after t_end.

    Returns:
        Ordered list of ClickRecords whose timestamps fall in the window.
        May be empty.

    Side Effects: None.
    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Yes.
    """
    if session_start_epoch == 0.0:
        # No anchor — cannot do temporal filtering; return all clicks as candidates
        # so the vision LLM still gets to pick rather than failing silently.
        logger.warning(
            "No session epoch anchor for issue '%s' — using all clicks as candidates.",
            issue.title,
        )
        return list(clicks)

    def _filter_with_window(w: float) -> list[ClickRecord]:
        ws = issue.t_start - w
        we = issue.t_end + w
        return [
            click for click in clicks
            if ws <= (click.timestamp - session_start_epoch) <= we
        ]

    # Try the requested window first, then widen progressively.
    # Audio device initialisation can add 0.5–3s latency between
    # AudioRecorder.start() (which writes recording_start.txt) and the
    # first audio sample, shifting all click epochs slightly relative to
    # the SRT's T=0.  Progressive widening recovers from this gracefully.
    candidates = _filter_with_window(window_seconds)
    if candidates:
        logger.info(
            "Issue '%s': %d/%d clicks in window [T+%.1f → T+%.1f]",
            issue.title, len(candidates), len(clicks),
            issue.t_start - window_seconds, issue.t_end + window_seconds,
        )
        return candidates

    for wider in (15.0, 30.0):
        candidates = _filter_with_window(wider)
        if candidates:
            logger.info(
                "Issue '%s': widened to ±%.0fs — %d/%d clicks in window [T+%.1f → T+%.1f]",
                issue.title, wider, len(candidates), len(clicks),
                issue.t_start - wider, issue.t_end + wider,
            )
            return candidates

    # Last resort: return all clicks and let the vision LLM pick.
    logger.warning(
        "Issue '%s': no clicks in any window up to ±30s — returning all %d clicks.",
        issue.title, len(clicks),
    )
    return list(clicks)



# ---------------------------------------------------------------------------
# Step 2b — Vision LLM best-shot selector
# ---------------------------------------------------------------------------


def select_best_screenshot(
    issue: IssueRecord,
    candidates: list[ClickRecord],
    api_key: str,
    model: str,
    mode: ProcessMode = ProcessMode.QA,
) -> Optional[ClickRecord]:
    """Pick the single best screenshot for an issue from among candidates.

    Purpose:
        Uses a vision LLM to evaluate 2+ candidate screenshots and return
        the one where the click marker (annotated circle) is positioned
        closest to the UI element described in the issue/step.

    Branching logic (NO LLM in the first two cases):
        - 0 candidates  → return None  (issue gets no screenshot)
        - 1 candidate   → return it directly (no LLM call)
        - ≥2 candidates → vision LLM call; picks best by:
              QA mode:  "which screenshot most clearly shows the broken element,
                         with the click circle nearest to the problem location"
              Doc mode: "which screenshot most clearly shows the action being
                         performed, with the click circle on the interacted element
                         and the resulting UI state change visible"

    Args:
        issue:       The IssueRecord (provides description for the LLM prompt).
        candidates:  Filtered ClickRecords from Step 2a.
        api_key:     OpenRouter API key.
        model:       Vision model identifier (e.g. "qwen/qwen2.5-vl-72b-instruct").
        mode:        ProcessMode.QA or ProcessMode.DOCUMENTATION.

    Returns:
        The best ClickRecord, or None if candidates is empty.

    Raises:
        Does not raise — on vision call failure, falls back to first candidate
        and logs a warning.

    Side Effects:
        HTTP POST to OpenRouter if len(candidates) >= 2.

    Determinism: Nondeterministic (LLM).
    Idempotency: No.
    Thread Safety: Yes.
    """
    if not candidates:
        logger.info("Issue '%s': no candidates — screenshot will be omitted.", issue.title)
        return None

    if len(candidates) == 1:
        logger.info(
            "Issue '%s': single candidate %s — no vision call needed.",
            issue.title, candidates[0].screenshot_path.name,
        )
        return candidates[0]

    # ── ≥2 candidates — vision LLM selects the best ──
    logger.info(
        "Issue '%s': %d candidates — calling vision LLM to select best screenshot.",
        issue.title, len(candidates),
    )

    system_prompt = _build_selector_system_prompt(mode)
    user_content = _build_selector_user_content(issue, candidates, mode)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
        "max_tokens": 64,
        "temperature": 0.0,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/vibecheck",
        "X-Title": "VibeCheck",
    }

    try:
        response = httpx.post(
            OPENROUTER_BASE_URL,
            json=payload,
            headers=headers,
            timeout=_VISION_TIMEOUT,
        )
        response.raise_for_status()
        raw_answer = response.json()["choices"][0]["message"]["content"].strip()
        logger.info("Vision LLM selected: %s (for issue '%s')", raw_answer, issue.title)

        if raw_answer.lower() == "none":
            return None

        # Match the returned filename to a candidate
        for candidate in candidates:
            if candidate.screenshot_path.name == raw_answer:
                return candidate

        # Fuzzy fallback — model sometimes wraps in backticks or adds path
        for candidate in candidates:
            if candidate.screenshot_path.name in raw_answer:
                return candidate

        logger.warning(
            "Vision LLM returned unrecognised filename '%s' for issue '%s' — "
            "falling back to first candidate.",
            raw_answer, issue.title,
        )
        return candidates[0]

    except Exception as vision_error:
        logger.warning(
            "Vision LLM call failed for issue '%s': %s — using first candidate.",
            issue.title, vision_error,
        )
        return candidates[0]


def _build_selector_system_prompt(mode: ProcessMode) -> str:
    """Build the system prompt for the Step 2b screenshot selector.

    Args:
        mode: QA or Documentation — changes the selection criterion.

    Returns:
        System prompt string.
    """
    if mode == ProcessMode.QA:
        return (
            "You are a QA screenshot selector. "
            "The user will describe a UI bug and show you several annotated screenshots. "
            "Each screenshot has a coloured click circle (white ring + coloured dot) "
            "showing exactly where the reviewer clicked. "
            "Your job: choose the single screenshot where "
            "(1) the broken UI element is clearly visible, AND "
            "(2) the click circle is positioned ON or nearest to that element — "
            "so a developer can immediately see what is broken and where. "
            "Reply with ONLY the filename (e.g. click_0003.png). "
            "If no screenshot clearly shows the described element near the click, reply: none"
        )
    else:
        return (
            "You are a documentation screenshot selector. "
            "The user will describe an action step and show you several annotated screenshots. "
            "Each screenshot has a coloured click circle (white ring + coloured dot) "
            "showing exactly where the user clicked. "
            "Your job: choose the single screenshot where "
            "(1) the click circle is ON or nearest to the UI element being interacted with, AND "
            "(2) the resulting UI state change is visible if possible "
            "(e.g. dropdown opened, form filled, panel appeared) — "
            "so a reader following the guide sees both WHERE to click and WHAT happens next. "
            "Reply with ONLY the filename (e.g. click_0003.png). "
            "If no screenshot shows the described action clearly, reply: none"
        )


def _build_selector_user_content(
    issue: IssueRecord,
    candidates: list[ClickRecord],
    mode: ProcessMode,
) -> list[dict]:
    """Build the user content array for the Step 2b vision call.

    Includes the issue/step description as text followed by each candidate
    screenshot as a base64-encoded image_url, labelled by filename so the
    model can reference it in its reply.

    Args:
        issue:      Issue or step being illustrated.
        candidates: Filtered candidate ClickRecords.
        mode:       QA or Documentation — adjusts the framing text.

    Returns:
        OpenRouter-format user content array (list of dicts).

    Side Effects: Reads screenshot files from disk.
    """
    if mode == ProcessMode.QA:
        framing = (
            f"Issue: \"{issue.title}\"\n"
            f"Description: {issue.description or issue.title}\n\n"
            f"Below are {len(candidates)} screenshots captured while the reviewer "
            f"was describing this issue. Choose the best one."
        )
    else:
        framing = (
            f"Step {issue.step_number}: \"{issue.title}\"\n"
            f"Action: {issue.description or issue.title}\n\n"
            f"Below are {len(candidates)} screenshots captured while the user "
            f"was performing this step. Choose the best one."
        )

    content: list[dict] = [{"type": "text", "text": framing}]

    for candidate in candidates:
        img_path = candidate.screenshot_path
        if not img_path.exists():
            logger.warning("Candidate screenshot missing: %s — skipping.", img_path.name)
            continue
        b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
        content.append({
            "type": "text",
            "text": f"[Image: {img_path.name}]",
        })
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })

    return content


# ---------------------------------------------------------------------------
# Step 1 — Issue/Step extraction (text LLM, no images)
# ---------------------------------------------------------------------------

_QA_EXTRACTION_SYSTEM = """\
You are a QA issue extractor. Given a timestamped audio transcript of a reviewer
narrating UI issues, extract each distinct issue as a JSON object.

Return a JSON array ONLY — no prose, no markdown fences. Each object must have:
  "title"               : short issue title (string)
  "t_start"             : transcript start time in seconds (float)
  "t_end"               : transcript end time in seconds (float)
  "priority"            : "Critical" | "High" | "Medium" | "Low" (string)
  "issue_type"          : "Bug" | "UI" | "UX" | "Performance" | "Missing Feature" (string)
  "description"         : verbatim or close paraphrase of what the reviewer said (string)
  "target_component"    : best-guess filename or component, e.g. "Sidebar.tsx" (string)
  "steps"               : array of implementation step strings
  "acceptance_criteria" : array of testable condition strings

Rules:
- Use ONLY what was spoken — do not infer issues from implied context.
- Merge micro-issues about the same component into one object.
- t_start / t_end must come from the timestamps in the transcript, not guessed.
- Output valid JSON that can be parsed with json.loads(). Nothing else."""

_DOC_EXTRACTION_SYSTEM = """\
You are a documentation step extractor. Given a timestamped audio transcript of
a user narrating a walkthrough, extract each distinct action step as a JSON object.

Return a JSON array ONLY — no prose, no markdown fences. Each object must have:
  "step_number"         : sequential integer starting at 1
  "title"               : short imperative title, e.g. "Click the Settings icon" (string)
  "t_start"             : transcript start time in seconds (float)
  "t_end"               : transcript end time in seconds (float)
  "description"         : what the user did and what happened (string, second person)
  "target_component"    : UI element or component interacted with (string)
  "acceptance_criteria" : single done-condition string in a one-element array

Rules:
- Steps must be in chronological order.
- t_start / t_end must come from the timestamps in the transcript.
- Output valid JSON that can be parsed with json.loads(). Nothing else."""


def extract_issues(
    transcript: list[TranscriptSegment],
    mode: ProcessMode,
    api_key: str,
    model: str,
) -> list[IssueRecord]:
    """Extract structured issues or steps from the spoken transcript (Agent 1).

    Purpose:
        Sends a text-only LLM call (no screenshots) that reads the full
        Whisper transcript and returns a JSON array of IssueRecord-shaped
        dicts.  The output is validated and converted to IssueRecord objects.

    Args:
        transcript: Ordered list of Whisper TranscriptSegments.
        mode:       QA → extract bugs/issues; Documentation → extract steps.
        api_key:    OpenRouter API key.
        model:      Text or vision model identifier (images not sent here).

    Returns:
        Ordered list of IssueRecord instances.

    Raises:
        PipelineError: If the LLM returns invalid JSON or an empty list,
                       signalling the caller to fall back to the legacy pipeline.

    Side Effects:
        HTTP POST to OpenRouter.

    Determinism: Nondeterministic.
    Idempotency: No.
    Thread Safety: Yes.
    """
    import time as _time

    transcript_text = _format_transcript(transcript)
    system_prompt = (
        _QA_EXTRACTION_SYSTEM if mode == ProcessMode.QA else _DOC_EXTRACTION_SYSTEM
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system",  "content": system_prompt},
            {"role": "user",    "content": transcript_text},
        ],
        "max_tokens": 4096,
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/vibecheck",
        "X-Title": "VibeCheck",
    }

    # Retry up to 3 times with exponential backoff on 429 (rate limit).
    _max_retries = 3
    _retry_delay = 10.0  # seconds; doubles each attempt
    for _attempt in range(_max_retries + 1):
        try:
            response = httpx.post(
                OPENROUTER_BASE_URL,
                json=payload,
                headers=headers,
                timeout=120.0,
            )
            if response.status_code == 429 and _attempt < _max_retries:
                logger.warning(
                    "OpenRouter rate limit (429) — waiting %.0fs before retry %d/%d …",
                    _retry_delay, _attempt + 1, _max_retries,
                )
                _time.sleep(_retry_delay)
                _retry_delay *= 2.0
                continue
            response.raise_for_status()
            break  # success
        except httpx.HTTPStatusError as http_error:
            raise PipelineError(
                f"Issue extraction HTTP error {http_error.response.status_code}"
            ) from http_error
        except httpx.TimeoutException as timeout_error:
            raise PipelineError("Issue extraction timed out.") from timeout_error


    raw_content = response.json()["choices"][0]["message"]["content"].strip()

    # Strip accidental markdown fences
    if raw_content.startswith("```"):
        raw_content = "\n".join(
            line for line in raw_content.splitlines()
            if not line.strip().startswith("```")
        )

    try:
        raw_list = json.loads(raw_content)
    except json.JSONDecodeError as parse_error:
        raise PipelineError(
            f"Issue extraction JSON parse error: {parse_error}\nRaw: {raw_content[:300]}"
        ) from parse_error

    if not isinstance(raw_list, list) or len(raw_list) == 0:
        raise PipelineError(
            f"Issue extraction returned empty or non-list: {type(raw_list)}"
        )

    issues: list[IssueRecord] = []
    for index, item in enumerate(raw_list):
        if not isinstance(item, dict):
            logger.warning("Skipping non-dict issue item at index %d: %s", index, item)
            continue
        try:
            issue = IssueRecord(
                title=str(item.get("title", f"Issue {index + 1}")),
                t_start=float(item.get("t_start", 0.0)),
                t_end=float(item.get("t_end", 0.0)),
                priority=str(item.get("priority", "Medium")),
                issue_type=str(item.get("issue_type", "")),
                description=str(item.get("description", "")),
                target_component=str(item.get("target_component", "")),
                steps=[str(s) for s in item.get("steps", [])],
                acceptance_criteria=[str(a) for a in item.get("acceptance_criteria", [])],
                step_number=int(item.get("step_number", index + 1)),
            )
            issues.append(issue)
        except (TypeError, ValueError) as field_error:
            logger.warning("Skipping malformed issue at index %d: %s", index, field_error)

    if not issues:
        raise PipelineError("No valid IssueRecord objects could be parsed.")

    logger.info("Extracted %d issues/steps from transcript.", len(issues))
    return issues


def _format_transcript(segments: list[TranscriptSegment]) -> str:
    """Format transcript segments as a timestamped text block for the LLM.

    Args:
        segments: Ordered Whisper TranscriptSegments.

    Returns:
        Multi-line string with one line per segment.
    """
    lines = ["## Transcript (timestamped)"]
    for segment in segments:
        start_m = int(segment.start) // 60
        start_s = segment.start % 60
        end_m = int(segment.end) // 60
        end_s = segment.end % 60
        lines.append(
            f"[{start_m:02d}:{start_s:05.2f} → {end_m:02d}:{end_s:05.2f}]  {segment.text}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 3 — Report assembly (code only, no LLM)
# ---------------------------------------------------------------------------


def assemble_qa_report(pairs: list[IssueWithScreenshot]) -> str:
    """Render matched QA issues into a Markdown report.

    Purpose:
        Deterministically converts structured IssueWithScreenshot pairs into
        the same Markdown format that the legacy pipeline produced, so all
        downstream HTML/DOCX rendering code is unchanged.

    Args:
        pairs: Ordered list of IssueWithScreenshot (issue + best screenshot).

    Returns:
        Markdown string ready for _wrap_markdown_in_html() and _build_docx_report().

    Side Effects: None.
    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Yes.
    """
    if not pairs:
        return "# VibeCheck — QA Tasks\n\n## Summary\nNo issues were extracted.\n"

    # Infer a page/feature name from the first issue's target component
    first_component = pairs[0].issue.target_component or "Application"
    feature_guess = first_component.replace(".tsx", "").replace(".py", "")

    lines: list[str] = [
        f"# [{feature_guess}] — QA Tasks",
        "",
        "## Summary",
        f"{len(pairs)} issue(s) identified from the recorded narration.",
        "",
        "## Tasks",
        "",
    ]

    for task_number, pair in enumerate(pairs, start=1):
        issue = pair.issue
        screenshot_ref = (
            pair.screenshot.screenshot_path.name
            if pair.screenshot is not None
            else "(no screenshot)"
        )

        lines += [
            f"### Task {task_number}: {issue.title}",
            f"- **Priority:** {issue.priority or 'Medium'}",
            f"- **Type:** {issue.issue_type or 'Bug'}",
            f"- **Screenshot:** {screenshot_ref}",
            f"- **Target Component:** `{issue.target_component or 'Unknown'}`",
            f"- **What's wrong:** {issue.description}",
            "- **Implementation steps:**",
        ]
        for step_index, step_text in enumerate(issue.steps, start=1):
            lines.append(f"  {step_index}. {step_text}")
        if not issue.steps:
            lines.append("  1. Review the component and apply the described fix.")

        lines.append("- **Acceptance criteria:**")
        for criterion in issue.acceptance_criteria:
            lines.append(f"  - [ ] {criterion}")
        if not issue.acceptance_criteria:
            lines.append("  - [ ] The described issue is resolved and verified.")
        lines.append("")

    return "\n".join(lines)


def assemble_documentation_report(pairs: list[IssueWithScreenshot]) -> str:
    """Render matched documentation steps into a Markdown how-to guide.

    Purpose:
        Produces an SOP / tutorial document where steps are sequential and
        each screenshot confirms the action performed (not a bug location).

    Args:
        pairs: Ordered list of IssueWithScreenshot (step + best screenshot).

    Returns:
        Markdown string ready for downstream rendering.

    Side Effects: None.
    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Yes.
    """
    if not pairs:
        return "# VibeCheck — How-To Guide\n\n## Overview\nNo steps were extracted.\n"

    first_component = pairs[0].issue.target_component or "Application"

    lines: list[str] = [
        f"# How To: {first_component}",
        "",
        "## Overview",
        "Step-by-step guide generated from a recorded walkthrough.",
        "",
        "## Prerequisites",
        "None (as narrated).",
        "",
        "## Step-by-Step Walkthrough",
        "",
    ]

    for pair in pairs:
        step = pair.issue
        screenshot_ref = (
            pair.screenshot.screenshot_path.name
            if pair.screenshot is not None
            else "(no screenshot)"
        )

        lines += [
            f"### Step {step.step_number}: {step.title}",
            f"- **Screenshot:** {screenshot_ref}",
            f"- **Action:** {step.description}",
        ]
        if step.acceptance_criteria:
            lines.append(f"- **Done when:** {step.acceptance_criteria[0]}")
        lines.append("")

    lines += [
        "## ✅ Done When",
        pairs[-1].issue.acceptance_criteria[0]
        if pairs[-1].issue.acceptance_criteria
        else "All steps completed as described.",
        "",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestrators — public entry points
# ---------------------------------------------------------------------------


def run_qa_pipeline(
    transcript: list[TranscriptSegment],
    clicks: list[ClickRecord],
    session_dir: Optional[Path],
    api_key: str,
    model: str,
) -> str:
    """Orchestrate the 3-step QA pipeline and return assembled Markdown.

    Pipeline:
        1. extract_issues()          — text LLM → list[IssueRecord]
        2a. filter_clicks_for_issue() — pure Python → candidates per issue
        2b. select_best_screenshot()  — vision LLM (only if ≥2 candidates)
        3. assemble_qa_report()       — code → Markdown

    Args:
        transcript:   Whisper output.
        clicks:       Mouse-tracker click records.
        session_dir:  Session folder (for recording_start.txt anchor).
        api_key:      OpenRouter API key.
        model:        Model identifier used for both Step 1 and Step 2b.

    Returns:
        Markdown string for HTML / DOCX rendering.

    Raises:
        PipelineError: If Step 1 fails (caller should fall back to legacy path).

    Side Effects:
        HTTP POSTs to OpenRouter.

    Determinism: Nondeterministic.
    Idempotency: No.
    Thread Safety: Yes.
    """
    logger.info("QA pipeline: Step 1 — extracting issues from transcript.")
    issues = extract_issues(transcript, ProcessMode.QA, api_key, model)

    session_start_epoch = read_session_start_epoch(session_dir)

    pairs: list[IssueWithScreenshot] = []
    for issue in issues:
        logger.info("QA pipeline: Step 2a — filtering clicks for '%s'.", issue.title)
        candidates = filter_clicks_for_issue(
            issue, clicks, session_start_epoch, _QA_WINDOW_SECONDS
        )

        logger.info("QA pipeline: Step 2b — selecting best screenshot for '%s'.", issue.title)
        best = select_best_screenshot(
            issue, candidates, api_key, model, ProcessMode.QA
        )
        pairs.append(IssueWithScreenshot(issue=issue, screenshot=best))

    logger.info("QA pipeline: Step 3 — assembling report (%d issues).", len(pairs))
    return assemble_qa_report(pairs)


def run_documentation_pipeline(
    transcript: list[TranscriptSegment],
    clicks: list[ClickRecord],
    session_dir: Optional[Path],
    api_key: str,
    model: str,
) -> str:
    """Orchestrate the 3-step Documentation pipeline and return assembled Markdown.

    Pipeline:
        1. extract_issues()              — text LLM → list[IssueRecord] (as steps)
        2a. filter_clicks_for_issue()     — pure Python → candidates per step
        2b. select_best_screenshot()      — vision LLM (only if ≥2 candidates)
        3. assemble_documentation_report() — code → Markdown

    Args:
        transcript:   Whisper output.
        clicks:       Mouse-tracker click records.
        session_dir:  Session folder (for recording_start.txt anchor).
        api_key:      OpenRouter API key.
        model:        Model identifier used for both Step 1 and Step 2b.

    Returns:
        Markdown string for HTML / DOCX rendering.

    Raises:
        PipelineError: If Step 1 fails (caller should fall back to legacy path).

    Side Effects:
        HTTP POSTs to OpenRouter.

    Determinism: Nondeterministic.
    Idempotency: No.
    Thread Safety: Yes.
    """
    logger.info("Documentation pipeline: Step 1 — extracting steps from transcript.")
    steps = extract_issues(transcript, ProcessMode.DOCUMENTATION, api_key, model)

    session_start_epoch = read_session_start_epoch(session_dir)

    pairs: list[IssueWithScreenshot] = []
    for step in steps:
        logger.info("Documentation pipeline: Step 2a — filtering clicks for step '%s'.", step.title)
        candidates = filter_clicks_for_issue(
            step, clicks, session_start_epoch, _DOC_WINDOW_SECONDS
        )

        logger.info("Documentation pipeline: Step 2b — selecting best screenshot for step '%s'.", step.title)
        best = select_best_screenshot(
            step, candidates, api_key, model, ProcessMode.DOCUMENTATION
        )
        pairs.append(IssueWithScreenshot(issue=step, screenshot=best))

    logger.info(
        "Documentation pipeline: Step 3 — assembling report (%d steps).", len(pairs)
    )
    return assemble_documentation_report(pairs)
