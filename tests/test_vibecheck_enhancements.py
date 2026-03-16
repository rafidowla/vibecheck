"""
Unit tests for VibeCheck enhancements:
  - HiDPI click marker coordinate scaling
  - QA and Documentation AI prompt builders
  - Jira client (mocked HTTP)

Run with:
    cd /Users/rdowla/Downloads/AiDev/BitBucket/non-core/VibeCheck
    source venv/bin/activate
    pip install pytest httpx
    pytest tests/ -v
"""

from __future__ import annotations

import base64
import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _import_module(name: str):
    """Import a module by dotted name safely."""
    import importlib
    return importlib.import_module(name)


# ===========================================================================
# 1. HiDPI Click Marker Coordinate Scaling
# ===========================================================================


class TestClickMarkerScaling(unittest.TestCase):
    """Verify that _capture_and_annotate draws the marker at the correct
    physical-pixel position on both Retina (2×) and standard (1×) displays.

    Side Effects: None (mss.grab and PIL drawing are fully mocked).
    """

    def _make_mock_image(self, width: int, height: int):
        """Create a tiny solid-color PIL Image for testing."""
        from PIL import Image
        return Image.new("RGB", (width, height), color=(40, 40, 40))

    def _run_capture(
        self,
        monitor: dict,
        physical_w: int,
        physical_h: int,
        click_x: int,
        click_y: int,
    ):
        """Run `_capture_and_annotate` against a mocked mss context.

        Returns:
            Tuple[int, int]: (local_x, local_y) pixel position the marker was
            drawn at, extracted from the mock draw.ellipse call.
        """
        from audit_tool.mouse_tracker import MouseTracker
        from PIL import Image, ImageDraw

        # Build a fake mss raw object
        mock_raw = MagicMock()
        mock_raw.size = (physical_w, physical_h)

        # bgra bytes for a solid black image
        mock_raw.bgra = bytes(physical_w * physical_h * 4)

        drawn_coords: list[Any] = []

        real_draw_ellipse = ImageDraw.ImageDraw.ellipse

        def patched_ellipse(self_draw, xy, **kwargs):
            drawn_coords.append(xy)
            real_draw_ellipse(self_draw, xy, **kwargs)

        import tempfile
        session_dir = Path(tempfile.mkdtemp())

        tracker = MouseTracker()
        tracker._session_dir = session_dir
        tracker._monitor_index = 1
        tracker._click_counter = 0

        # Build a mock sct
        mock_sct = MagicMock()
        mock_sct.monitors = [None, monitor]
        mock_sct.grab.return_value = mock_raw
        tracker._sct = mock_sct

        with patch.object(ImageDraw.ImageDraw, "ellipse", patched_ellipse):
            tracker._capture_and_annotate(click_x, click_y)

        if not drawn_coords:
            raise AssertionError("ellipse was never called")

        # The new ripple marker draws via two ImageDraw instances:
        #   drawn_coords[0] = semi-transparent indigo fill (on overlay)
        #   drawn_coords[1] = outer white ring  (on composited image)
        #   drawn_coords[2] = indigo outline ring (radius = scaled_radius)  ← use this
        #   drawn_coords[3] = white centre dot
        # We extract the centre from the indigo outline ring (index 2) because
        # it uses CLICK_MARKER_RADIUS directly, which is what the tests target.
        box = drawn_coords[2]
        from audit_tool.config import CLICK_MARKER_RADIUS
        scale_x = physical_w / monitor["width"]
        scaled_radius = int(CLICK_MARKER_RADIUS * scale_x)
        local_x = box[0] + scaled_radius
        local_y = box[1] + scaled_radius
        return local_x, local_y

    def test_standard_display_no_scaling(self):
        """On a 1× display, logical coords equal physical coords."""
        monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        local_x, local_y = self._run_capture(monitor, 1920, 1080, 500, 300)
        self.assertEqual(local_x, 500, "1× display: X should be unchanged")
        self.assertEqual(local_y, 300, "1× display: Y should be unchanged")

    def test_retina_display_2x_scaling(self):
        """On a 2× Retina display, coords should be doubled."""
        monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        # mss captures at physical 3840×2160
        local_x, local_y = self._run_capture(monitor, 3840, 2160, 500, 300)
        self.assertEqual(local_x, 1000, "2× display: X should be 500 * 2 = 1000")
        self.assertEqual(local_y, 600, "2× display: Y should be 300 * 2 = 600")

    def test_secondary_monitor_offset(self):
        """Coordinates are translated relative to the monitor's top-left origin."""
        monitor = {"left": 1920, "top": 0, "width": 1920, "height": 1080}
        # Click at absolute (2420, 300) → local (500, 300) on 1× display
        local_x, local_y = self._run_capture(monitor, 1920, 1080, 2420, 300)
        self.assertEqual(local_x, 500)
        self.assertEqual(local_y, 300)

    def test_clamping_out_of_bounds(self):
        """A click outside the monitor bounds is clamped to image edges."""
        monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        # Click beyond right edge
        local_x, local_y = self._run_capture(monitor, 1920, 1080, 9999, 300)
        self.assertLessEqual(local_x, 1919, "Out-of-bounds X must be clamped")


# ===========================================================================
# 2. Process Mode Prompts
# ===========================================================================


class TestPromptBuilders(unittest.TestCase):
    """Verify that each mode's prompt contains its key structural markers.

    These are whitebox tests — they verify the prompt contracts so that
    accidental edits do not silently break the AI output format.
    """

    def _sample_transcript(self) -> str:
        return "[00:00 → 00:05] The login button is misaligned on Safari."

    def _sample_clicks(self) -> list:
        click = MagicMock()
        click.index = 0
        click.x = 640
        click.y = 400
        click.timestamp = 1_700_000_000.0
        return [click]

    def test_qa_prompt_contains_task_structure(self):
        """QA prompt must instruct the model to produce ### Task N blocks."""
        from audit_tool.report_generator import _build_qa_prompt
        prompt = _build_qa_prompt(self._sample_transcript(), self._sample_clicks())

        self.assertIn("### Task", prompt, "QA prompt must reference ### Task N format")
        self.assertIn("Implementation steps", prompt)
        self.assertIn("Acceptance criteria", prompt)
        self.assertIn("Priority", prompt)

    def test_qa_prompt_references_ai_agents(self):
        """QA prompt should mention AI coding agents by name."""
        from audit_tool.report_generator import _build_qa_prompt
        prompt = _build_qa_prompt(self._sample_transcript(), self._sample_clicks())
        self.assertIn("Antigravity", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("Cursor", prompt)

    def test_documentation_prompt_contains_step_structure(self):
        """Documentation prompt must instruct second-person SOP format."""
        from audit_tool.report_generator import _build_documentation_prompt
        prompt = _build_documentation_prompt(
            self._sample_transcript(), self._sample_clicks()
        )

        self.assertIn("Step-by-Step Walkthrough", prompt)
        self.assertIn("How-To Guide", prompt)
        self.assertIn("second person", prompt)
        self.assertIn("Acceptance Checklist", prompt)

    def test_documentation_prompt_is_not_qa(self):
        """Documentation prompt must NOT contain QA-specific bug language."""
        from audit_tool.report_generator import _build_documentation_prompt
        prompt = _build_documentation_prompt(
            self._sample_transcript(), self._sample_clicks()
        )
        self.assertNotIn("### Task", prompt)
        self.assertNotIn("Implementation steps", prompt)

    def test_prompts_include_transcript(self):
        """Both prompts should inject the transcript text."""
        from audit_tool.report_generator import _build_qa_prompt, _build_documentation_prompt
        transcript = self._sample_transcript()
        clicks = self._sample_clicks()

        qa_prompt = _build_qa_prompt(transcript, clicks)
        doc_prompt = _build_documentation_prompt(transcript, clicks)

        self.assertIn("login button", qa_prompt)
        self.assertIn("login button", doc_prompt)


# ===========================================================================
# 3. Jira Client (mocked HTTP)
# ===========================================================================


class TestJiraClientCreateIssue(unittest.TestCase):
    """Verify that create_issue builds the correct request and returns the key."""

    def _make_config(self):
        from audit_tool.config import JiraConfig
        return JiraConfig(
            base_url="https://test.atlassian.net",
            email="tester@example.com",
            api_token="tok123",
            project_key="TEST",
            issue_type="Task",
        )

    def test_create_issue_returns_key(self):
        """create_issue should return the 'key' field from the Jira response."""
        from audit_tool.jira_client import create_issue, JiraIssuePayload

        payload = JiraIssuePayload(
            summary="Fix the login button alignment",
            description_markdown="## Problem\nThe button is off-centre.",
            priority="High",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {"key": "TEST-99"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_response) as mock_post:
            key = create_issue(self._make_config(), payload)

        self.assertEqual(key, "TEST-99")
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        request_body = call_kwargs["json"]
        self.assertEqual(request_body["fields"]["project"]["key"], "TEST")
        self.assertEqual(request_body["fields"]["issuetype"]["name"], "Task")

    def test_create_issue_sends_auth_header(self):
        """Authorization header must use Base64-encoded email:token."""
        from audit_tool.jira_client import create_issue, JiraIssuePayload

        payload = JiraIssuePayload(summary="Test", description_markdown="desc")
        mock_response = MagicMock()
        mock_response.json.return_value = {"key": "TEST-1"}
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.post", return_value=mock_response) as mock_post:
            create_issue(self._make_config(), payload)

        headers = mock_post.call_args[1]["headers"]
        self.assertIn("Authorization", headers)
        auth = headers["Authorization"]
        self.assertTrue(auth.startswith("Basic "))
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        self.assertEqual(decoded, "tester@example.com:tok123")

    def test_create_issue_raises_on_http_error(self):
        """create_issue must raise JiraClientError on non-2xx responses."""
        import httpx
        from audit_tool.jira_client import create_issue, JiraClientError, JiraIssuePayload

        payload = JiraIssuePayload(summary="Test", description_markdown="desc")
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        http_error = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=mock_response
        )
        mock_response.raise_for_status.side_effect = http_error

        with patch("httpx.post", return_value=mock_response):
            with self.assertRaises(JiraClientError) as ctx:
                create_issue(self._make_config(), payload)

        self.assertIn("401", ctx.exception.message)
        self.assertEqual(ctx.exception.status_code, 401)


class TestJiraMarkdownToAdf(unittest.TestCase):
    """Verify the ADF conversion produces valid structure."""

    def test_heading_node(self):
        """A Markdown heading must produce an ADF heading node."""
        from audit_tool.jira_client import _markdown_to_adf
        adf = _markdown_to_adf("## My Heading")
        heading_nodes = [
            n for n in adf["content"] if n["type"] == "heading"
        ]
        self.assertTrue(len(heading_nodes) >= 1)
        self.assertEqual(heading_nodes[0]["attrs"]["level"], 2)

    def test_bullet_node(self):
        """A Markdown bullet must produce an ADF bulletList node."""
        from audit_tool.jira_client import _markdown_to_adf
        adf = _markdown_to_adf("- Fix the alignment")
        bullet_nodes = [
            n for n in adf["content"] if n["type"] == "bulletList"
        ]
        self.assertTrue(len(bullet_nodes) >= 1)

    def test_truncation(self):
        """Very long text should be truncated to _MAX_DESCRIPTION_CHARS."""
        from audit_tool.jira_client import _markdown_to_adf, _MAX_DESCRIPTION_CHARS
        long_text = "x" * (_MAX_DESCRIPTION_CHARS + 500)
        adf = _markdown_to_adf(long_text)
        # Reconstruct text content from nodes
        all_text = json.dumps(adf)
        self.assertIn("[truncated]", all_text)


if __name__ == "__main__":
    unittest.main()
