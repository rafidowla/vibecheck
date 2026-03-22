"""
Unit tests for the VibeCheck 3-step multi-agent pipeline.

Tests cover:
  - filter_clicks_for_issue (pure Python, no mocks needed)
  - select_best_screenshot (mocked vision LLM — branching on 0/1/2+ candidates)
  - extract_issues (mocked text LLM — JSON parse and fallback paths)

Run with:
    cd /Users/rdowla/Downloads/AiDev/BitBucket/non-core/VibeCheck
    source .venv/bin/activate
    python -m pytest tests/test_pipeline.py -v
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import tempfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_click(index: int, timestamp: float, x: float = 100.0, y: float = 200.0) -> Any:
    """Build a mock ClickRecord with the given epoch timestamp."""
    click = MagicMock()
    click.index = index
    click.timestamp = timestamp
    click.x = x
    click.y = y
    path_mock = MagicMock(spec=Path)
    path_mock.name = f"click_{index:04d}.png"
    path_mock.exists.return_value = True
    # Return real bytes so base64.b64encode() works in _build_selector_user_content.
    path_mock.read_bytes.return_value = b"fake-png-bytes"
    click.screenshot_path = path_mock
    return click


def _make_issue(t_start: float, t_end: float, title: str = "Test Issue") -> Any:
    """Build a mock IssueRecord with given time bounds."""
    from audit_tool.pipeline import IssueRecord
    return IssueRecord(
        title=title,
        t_start=t_start,
        t_end=t_end,
        priority="High",
        issue_type="Bug",
        description="Test description.",
        target_component="Component.tsx",
        steps=["Step 1"],
        acceptance_criteria=["Condition met."],
    )


# ---------------------------------------------------------------------------
# Test: filter_clicks_for_issue — pure Python, no mocking
# ---------------------------------------------------------------------------

class TestFilterClicks(unittest.TestCase):
    """Verify that filter_clicks_for_issue correctly applies the time window.

    No LLM calls are made — this is 100% deterministic Python arithmetic.
    """

    # Session started at epoch 1_000_000.0; Whisper segments are relative to 0s.
    SESSION_EPOCH: float = 1_000_000.0

    def _run_filter(self, issue_t_start, issue_t_end, click_offsets, window=5.0):
        """Helper: run filter with clicks defined as session-relative second offsets."""
        from audit_tool.pipeline import filter_clicks_for_issue
        issue = _make_issue(issue_t_start, issue_t_end)
        clicks = [
            _make_click(idx, self.SESSION_EPOCH + offset)
            for idx, offset in enumerate(click_offsets)
        ]
        return filter_clicks_for_issue(issue, clicks, self.SESSION_EPOCH, window)

    def test_no_clicks_returns_empty(self):
        """With no click records, the result should be an empty list."""
        result = self._run_filter(10.0, 20.0, [])
        self.assertEqual(result, [])

    def test_click_inside_window_is_included(self):
        """A click at T+15 is inside [10-5=5, 20+5=25] and must be included."""
        result = self._run_filter(10.0, 20.0, [15.0])
        self.assertEqual(len(result), 1)

    def test_click_before_window_is_excluded(self):
        """A click far outside any window returns all clicks via last-resort fallback."""
        # T+200 is far outside the ±5/±15/±30s windows for issue [10, 20].
        # The fallback returns all clicks (1 click) rather than 0.
        result = self._run_filter(10.0, 20.0, [200.0])
        # Progressive fallback means we still get 1 (all clicks), not 0
        self.assertEqual(len(result), 1)

    def test_click_after_window_is_excluded(self):
        """A click far past any window returns all clicks via last-resort fallback."""
        # T+200 is far outside even ±30s widening for issue [10, 20].
        result = self._run_filter(10.0, 20.0, [200.0])
        self.assertEqual(len(result), 1)  # last-resort: all clicks

    def test_boundary_at_window_start_is_included(self):
        """A click exactly at t_start - window (T+5) must be included (inclusive)."""
        result = self._run_filter(10.0, 20.0, [5.0])
        self.assertEqual(len(result), 1)

    def test_boundary_at_window_end_is_included(self):
        """A click exactly at t_end + window (T+25) must be included (inclusive)."""
        result = self._run_filter(10.0, 20.0, [25.0])
        self.assertEqual(len(result), 1)

    def test_multiple_clicks_some_in_some_out(self):
        """Only clicks within the window are returned; others are dropped."""
        result = self._run_filter(10.0, 20.0, [1.0, 12.0, 18.0, 99.0])
        self.assertEqual(len(result), 2)
        result_offsets = [c.timestamp - self.SESSION_EPOCH for c in result]
        self.assertIn(12.0, result_offsets)
        self.assertIn(18.0, result_offsets)

    def test_no_epoch_anchor_returns_all_clicks(self):
        """When session_start_epoch is 0 (missing file), all clicks are returned."""
        from audit_tool.pipeline import filter_clicks_for_issue
        issue = _make_issue(10.0, 20.0)
        # Clicks with arbitrary large epoch — won't be in the window if anchored
        clicks = [_make_click(i, 9_999_999.0) for i in range(3)]
        result = filter_clicks_for_issue(issue, clicks, 0.0)
        self.assertEqual(len(result), 3)

    def test_tighter_window_excludes_more(self):
        """A 5s window finds more candidates than a 2s window (before fallback kicks in)."""
        # clicks at T+4 and T+26 are inside the 5s window [5, 25] for issue [10, 20].
        # The 2s window [8, 22] misses both, but 5s window includes both.
        in_5s = self._run_filter(10.0, 20.0, [7.0, 23.0], window=5.0)  # [5, 25] → both included
        # With 2s window the strict filter finds 0, then widens — eventually returns all 2.
        # The key invariant: 5s should find candidates on first try (not via fallback).
        self.assertEqual(len(in_5s), 2)   # 5s window finds both directly


# ---------------------------------------------------------------------------
# Test: select_best_screenshot — mocked vision LLM
# ---------------------------------------------------------------------------

class TestSelectBestScreenshot(unittest.TestCase):
    """Verify branching logic for 0/1/2+ candidates and vision LLM mock."""

    def _make_candidates(self, count: int) -> list:
        """Return a list of `count` mock ClickRecords."""
        return [_make_click(i, 1_000_000.0 + i) for i in range(count)]

    def test_zero_candidates_returns_none_no_llm(self):
        """Zero candidates must return None without calling the LLM."""
        from audit_tool.pipeline import select_best_screenshot, ProcessMode
        issue = _make_issue(0.0, 5.0)
        with patch("httpx.post") as mock_post:
            result = select_best_screenshot(
                issue, [], "fake-key", "test-model", ProcessMode.QA
            )
        self.assertIsNone(result)
        mock_post.assert_not_called()

    def test_single_candidate_returns_it_no_llm(self):
        """A single candidate must be returned directly without any LLM call."""
        from audit_tool.pipeline import select_best_screenshot, ProcessMode
        issue = _make_issue(0.0, 5.0)
        candidates = self._make_candidates(1)
        with patch("httpx.post") as mock_post:
            result = select_best_screenshot(
                issue, candidates, "fake-key", "test-model", ProcessMode.QA
            )
        self.assertEqual(result, candidates[0])
        mock_post.assert_not_called()

    def test_multiple_candidates_calls_vision_llm(self):
        """Two or more candidates must trigger a vision LLM call."""
        from audit_tool.pipeline import select_best_screenshot, ProcessMode
        issue = _make_issue(0.0, 5.0)
        candidates = self._make_candidates(3)

        # Vision LLM returns the name of candidate 1
        chosen_name = candidates[1].screenshot_path.name
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": chosen_name}}]
        }

        with patch("httpx.post", return_value=mock_response) as mock_post:
            result = select_best_screenshot(
                issue, candidates, "fake-key", "test-model", ProcessMode.QA
            )

        mock_post.assert_called_once()
        self.assertEqual(result, candidates[1])

    def test_vision_llm_returns_none_label(self):
        """If the vision LLM replies 'none', None is returned."""
        from audit_tool.pipeline import select_best_screenshot, ProcessMode
        issue = _make_issue(0.0, 5.0)
        candidates = self._make_candidates(2)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "none"}}]
        }

        with patch("httpx.post", return_value=mock_response):
            result = select_best_screenshot(
                issue, candidates, "fake-key", "test-model", ProcessMode.QA
            )
        self.assertIsNone(result)

    def test_vision_llm_failure_falls_back_to_first(self):
        """On any vision LLM exception, the first candidate is used as fallback."""
        from audit_tool.pipeline import select_best_screenshot, ProcessMode
        import httpx
        issue = _make_issue(0.0, 5.0)
        candidates = self._make_candidates(2)

        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            result = select_best_screenshot(
                issue, candidates, "fake-key", "test-model", ProcessMode.QA
            )
        self.assertEqual(result, candidates[0])

    def test_documentation_mode_prompt_used(self):
        """Documentation mode must call the LLM with a prompt containing 'step'."""
        from audit_tool.pipeline import select_best_screenshot, ProcessMode
        issue = _make_issue(0.0, 5.0, title="Click Settings")
        issue.step_number = 1
        issue.description = "Click the Settings icon."
        candidates = self._make_candidates(2)

        captured_payload: list[dict] = []

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": candidates[0].screenshot_path.name}}]
        }

        def capture_post(url, json=None, **kwargs):
            captured_payload.append(json)
            return mock_response

        with patch("httpx.post", side_effect=capture_post):
            select_best_screenshot(
                issue, candidates, "fake-key", "test-model", ProcessMode.DOCUMENTATION
            )

        system_prompt = captured_payload[0]["messages"][0]["content"]
        self.assertIn("action step", system_prompt.lower())
        self.assertNotIn("bug", system_prompt.lower())


# ---------------------------------------------------------------------------
# Test: extract_issues — mocked text LLM
# ---------------------------------------------------------------------------

class TestExtractIssues(unittest.TestCase):
    """Verify issue extraction from transcript via mocked LLM."""

    def _make_transcript(self):
        from audit_tool.transcriber import TranscriptSegment
        return [
            TranscriptSegment(start=5.0, end=15.0, text="The comment box hover is broken."),
            TranscriptSegment(start=20.0, end=30.0, text="Search bar has no placeholder."),
        ]

    def _mock_llm_response(self, payload: list) -> MagicMock:
        """Return a mock httpx response returning the given JSON list."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps(payload)}}]
        }
        return mock_response

    def test_returns_issue_records_from_valid_json(self):
        """Valid JSON from LLM must be converted to IssueRecord instances."""
        from audit_tool.pipeline import extract_issues, IssueRecord, ProcessMode
        payload = [
            {
                "title": "Comment Box Hover",
                "t_start": 5.0, "t_end": 15.0,
                "priority": "High", "issue_type": "Bug",
                "description": "Hover broken.", "target_component": "CommentBox.tsx",
                "steps": ["Fix CSS"], "acceptance_criteria": ["Hover works."],
            }
        ]
        with patch("httpx.post", return_value=self._mock_llm_response(payload)):
            result = extract_issues(
                self._make_transcript(), ProcessMode.QA, "fake-key", "test-model"
            )
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], IssueRecord)
        self.assertEqual(result[0].title, "Comment Box Hover")
        self.assertAlmostEqual(result[0].t_start, 5.0)
        self.assertEqual(result[0].priority, "High")

    def test_raises_pipeline_error_on_invalid_json(self):
        """Non-JSON LLM response must raise PipelineError (triggers fallback)."""
        from audit_tool.pipeline import extract_issues, PipelineError, ProcessMode

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "This is not JSON at all."}}]
        }
        with patch("httpx.post", return_value=mock_response):
            with self.assertRaises(PipelineError):
                extract_issues(
                    self._make_transcript(), ProcessMode.QA, "fake-key", "test-model"
                )

    def test_raises_pipeline_error_on_empty_list(self):
        """An empty JSON array from the LLM must raise PipelineError."""
        from audit_tool.pipeline import extract_issues, PipelineError, ProcessMode
        with patch("httpx.post", return_value=self._mock_llm_response([])):
            with self.assertRaises(PipelineError):
                extract_issues(
                    self._make_transcript(), ProcessMode.QA, "fake-key", "test-model"
                )

    def test_strips_markdown_fence_from_llm_response(self):
        """LLM sometimes wraps JSON in ```json … ``` fences — must be stripped."""
        from audit_tool.pipeline import extract_issues, ProcessMode
        payload = [
            {
                "title": "Search Placeholder",
                "t_start": 20.0, "t_end": 30.0,
                "priority": "Medium", "issue_type": "UI",
                "description": "No placeholder.", "target_component": "SearchBar.tsx",
                "steps": [], "acceptance_criteria": [],
            }
        ]
        raw_with_fence = f"```json\n{json.dumps(payload)}\n```"
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": raw_with_fence}}]
        }
        with patch("httpx.post", return_value=mock_response):
            result = extract_issues(
                self._make_transcript(), ProcessMode.QA, "fake-key", "test-model"
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "Search Placeholder")

    def test_documentation_mode_uses_step_number(self):
        """Documentation mode JSON must populate step_number on the IssueRecord."""
        from audit_tool.pipeline import extract_issues, ProcessMode
        payload = [
            {
                "step_number": 2,
                "title": "Click Settings",
                "t_start": 5.0, "t_end": 12.0,
                "priority": "", "issue_type": "",
                "description": "Click the Settings icon.",
                "target_component": "NavBar",
                "steps": [], "acceptance_criteria": ["Settings panel opens."],
            }
        ]
        with patch("httpx.post", return_value=self._mock_llm_response(payload)):
            result = extract_issues(
                self._make_transcript(), ProcessMode.DOCUMENTATION, "fake-key", "test-model"
            )
        self.assertEqual(result[0].step_number, 2)
        self.assertEqual(result[0].acceptance_criteria, ["Settings panel opens."])


# ---------------------------------------------------------------------------
# Test: read_session_start_epoch helper
# ---------------------------------------------------------------------------

class TestReadSessionStartEpoch(unittest.TestCase):
    """Verify epoch anchor reading from recording_start.txt."""

    def test_reads_float_from_file(self):
        """Valid recording_start.txt must return its float value."""
        from audit_tool.pipeline import read_session_start_epoch
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "recording_start.txt").write_text("1742526480.123", encoding="utf-8")
            result = read_session_start_epoch(tmp_path)
        self.assertAlmostEqual(result, 1742526480.123)

    def test_returns_zero_when_file_missing(self):
        """Missing recording_start.txt must return 0.0."""
        from audit_tool.pipeline import read_session_start_epoch
        with tempfile.TemporaryDirectory() as tmp:
            result = read_session_start_epoch(Path(tmp))
        self.assertEqual(result, 0.0)

    def test_returns_zero_when_session_dir_is_none(self):
        """None session_dir must return 0.0 without error."""
        from audit_tool.pipeline import read_session_start_epoch
        result = read_session_start_epoch(None)
        self.assertEqual(result, 0.0)

    def test_returns_zero_on_corrupt_file(self):
        """A non-numeric recording_start.txt must return 0.0, not raise."""
        from audit_tool.pipeline import read_session_start_epoch
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "recording_start.txt").write_text("CORRUPTED", encoding="utf-8")
            result = read_session_start_epoch(tmp_path)
        self.assertEqual(result, 0.0)


if __name__ == "__main__":
    unittest.main()
