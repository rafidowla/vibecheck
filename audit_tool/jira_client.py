"""
Jira REST API adapter for VibeCheck.

Purpose:
    Provides a thin, self-contained client for creating Jira issues and
    attaching screenshots.  Designed for Jira Cloud (Atlassian REST API v3).
    All authentication uses HTTP Basic Auth with an API token — passwords are
    never accepted or stored.

Inputs:
    Requires a populated ``JiraConfig`` from ``audit_tool.config``.

Outputs:
    - ``create_issue`` returns the created Jira issue key (e.g. ``PROJ-42``).
    - ``attach_files_to_issue`` uploads one or more files to an existing issue.
    - ``push_session_to_jira`` is the high-level entry point that orchestrates
      both operations and returns a list of created issue keys.

Error Behavior:
    - All HTTP errors are caught and re-raised as ``JiraClientError``.
    - Raw Jira error messages are never propagated to the GUI — only safe,
      human-readable summaries are exposed.
    - ``JiraClientError`` carries a ``status_code`` for structured handling.

Side Effects:
    - HTTP POST/PUT calls to the configured Jira instance.
    - No local file writes.

Determinism: Nondeterministic (depends on Jira server state).
Idempotency: No — each call creates new issues.
Thread Safety: Yes — no shared mutable state.
Concurrency: Uses synchronous ``httpx`` calls (blocking per issue).
Performance: One HTTP call per issue + one multipart upload per issue.
    For large sessions (>10 tasks) this may take several seconds.
Observability:
    - Logs created issue keys at INFO level.
    - Logs HTTP errors at ERROR level (without leaking tokens).
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from audit_tool.config import JiraConfig, ProcessMode

logger = logging.getLogger(__name__)

# Jira Cloud REST API v3 base paths (relative to JiraConfig.base_url)
_REST_BASE = "/rest/api/3"
_ISSUE_PATH = f"{_REST_BASE}/issue"
_ATTACH_PATH = f"{_REST_BASE}/issue/{{issue_key}}/attachments"

# Maximum description length Jira accepts (ADF text node limit is ~32 KB)
_MAX_DESCRIPTION_CHARS: int = 30_000


class JiraClientError(Exception):
    """Raised when a Jira API call fails.

    Attributes:
        message: Safe, user-facing description of the failure.
        status_code: HTTP status code, or 0 for network/parse errors.
    """

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class JiraIssuePayload:
    """Data required to create a single Jira issue.

    Attributes:
        summary: Issue title (Jira summary field).
        description_markdown: Plain Markdown body text.  Converted to
            Atlassian Document Format (ADF) before submission.
        labels: Optional list of label strings.
        priority: Jira priority name (e.g. "High", "Medium").
        attachments: Paths to image files to attach after creation.
        task_number: Sequential task number for logging. (optional)
    """

    summary: str
    description_markdown: str
    labels: list[str] = field(default_factory=list)
    priority: str = "Medium"
    attachments: list[Path] = field(default_factory=list)
    task_number: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_issue(config: JiraConfig, payload: JiraIssuePayload) -> str:
    """Create a single Jira issue and return its key.

    Purpose:
        Posts a new issue to the Jira Cloud REST API using the project and
        issue type from ``JiraConfig``.

    Inputs:
        config (JiraConfig): Connection and auth settings.
        payload (JiraIssuePayload): Issue content.

    Outputs:
        str: Created issue key, e.g. ``"PROJ-42"``.

    Error Behavior:
        Raises ``JiraClientError`` on HTTP errors or unexpected response shape.
        The raw Jira error body is logged at DEBUG level but not propagated.

    Side Effects:
        HTTP POST to ``{config.base_url}/rest/api/3/issue``.

    Determinism: Nondeterministic (server-assigned key).
    Idempotency: No — each call creates a new issue.
    Thread Safety: Yes.
    """
    url = f"{config.base_url}{_ISSUE_PATH}"
    headers = _auth_headers(config)
    headers["Content-Type"] = "application/json"

    body = {
        "fields": {
            "project": {"key": config.project_key},
            "summary": payload.summary[:255],  # Jira limit
            "description": _markdown_to_adf(payload.description_markdown),
            "issuetype": {"name": config.issue_type},
            "priority": {"name": payload.priority},
            **({"labels": payload.labels} if payload.labels else {}),
        }
    }

    try:
        response = httpx.post(url, json=body, headers=headers, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPStatusError as http_error:
        logger.debug("Jira create-issue error body: %s", http_error.response.text)
        raise JiraClientError(
            f"Jira rejected issue creation (HTTP {http_error.response.status_code}). "
            "Check your project key, issue type, and API token.",
            status_code=http_error.response.status_code,
        ) from http_error
    except httpx.RequestError as network_error:
        raise JiraClientError(
            f"Could not reach Jira at '{config.base_url}': {network_error}"
        ) from network_error

    data = response.json()
    issue_key: str = data.get("key", "")
    if not issue_key:
        raise JiraClientError(
            "Jira returned a success response but did not include an issue key."
        )

    logger.info("Created Jira issue: %s — %s", issue_key, payload.summary[:60])
    return issue_key


def attach_files_to_issue(
    config: JiraConfig,
    issue_key: str,
    file_paths: list[Path],
) -> None:
    """Upload one or more files as attachments to an existing Jira issue.

    Purpose:
        Uses the Jira multipart attachment endpoint to embed screenshots
        directly in the issue for QA traceability.

    Inputs:
        config (JiraConfig): Connection and auth settings.
        issue_key (str): The Jira issue key to attach files to.
        file_paths (list[Path]): Absolute paths to files to upload.  Files
            that do not exist are skipped with a warning.

    Outputs:
        None.

    Error Behavior:
        Raises ``JiraClientError`` on HTTP errors.
        Missing files are logged as warnings and skipped, not raised.

    Side Effects:
        HTTP POST (multipart) to
        ``{config.base_url}/rest/api/3/issue/{issue_key}/attachments``.

    Determinism: Nondeterministic.
    Idempotency: No — Jira allows duplicate attachments.
    Thread Safety: Yes.
    """
    url = _ATTACH_PATH.format(issue_key=issue_key).join(
        [config.base_url, ""]
    ).rstrip("/")
    # Rebuild the URL properly
    url = f"{config.base_url}{_ATTACH_PATH.format(issue_key=issue_key)}"
    headers = _auth_headers(config)
    headers["X-Atlassian-Token"] = "no-check"  # Required to bypass XSRF

    for file_path in file_paths:
        if not file_path.exists():
            logger.warning("Skipping missing attachment: %s", file_path)
            continue
        try:
            with file_path.open("rb") as fh:
                response = httpx.post(
                    url,
                    headers=headers,
                    files={"file": (file_path.name, fh, "image/png")},
                    timeout=60.0,
                )
                response.raise_for_status()
            logger.info("Attached %s → %s", file_path.name, issue_key)
        except httpx.HTTPStatusError as http_error:
            logger.debug("Jira attach error body: %s", http_error.response.text)
            raise JiraClientError(
                f"Failed to attach '{file_path.name}' to {issue_key} "
                f"(HTTP {http_error.response.status_code}).",
                status_code=http_error.response.status_code,
            ) from http_error
        except httpx.RequestError as network_error:
            raise JiraClientError(
                f"Network error while attaching '{file_path.name}': {network_error}"
            ) from network_error


def push_session_to_jira(
    config: JiraConfig,
    payloads: list[JiraIssuePayload],
) -> list[str]:
    """Create multiple Jira issues and attach their screenshots.

    Purpose:
        High-level orchestrator called by ``report_generator.push_to_jira``.
        Iterates over the extracted task/step payloads, creates one issue each,
        then attaches the corresponding screenshots.

    Inputs:
        config (JiraConfig): Connection and auth settings.
        payloads (list[JiraIssuePayload]): One payload per issue to create.

    Outputs:
        list[str]: Created issue keys in order (e.g. ``["PROJ-42", "PROJ-43"]``).
        Failed issues are skipped and logged; their keys are omitted.

    Error Behavior:
        Per-issue errors are caught and logged.  The function always returns
        the keys of successfully created issues rather than raising.

    Side Effects:
        Multiple HTTP calls to the Jira instance.

    Determinism: Nondeterministic.
    Idempotency: No.
    Thread Safety: Yes (called from background thread in main.py).
    Performance: O(n) HTTP calls where n = number of payloads.
    Observability: Logs each created key at INFO; errors at ERROR.
    """
    created_keys: list[str] = []

    for payload in payloads:
        try:
            issue_key = create_issue(config, payload)
            created_keys.append(issue_key)

            if payload.attachments:
                try:
                    attach_files_to_issue(config, issue_key, payload.attachments)
                except JiraClientError as attach_error:
                    logger.error(
                        "Attachment failed for %s: %s", issue_key, attach_error.message
                    )
        except JiraClientError as create_error:
            logger.error(
                "Failed to create Jira issue (task #%d): %s",
                payload.task_number,
                create_error.message,
            )

    logger.info(
        "Jira push complete: %d / %d issues created — %s",
        len(created_keys),
        len(payloads),
        ", ".join(created_keys) if created_keys else "none",
    )
    return created_keys


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _auth_headers(config: JiraConfig) -> dict[str, str]:
    """Build HTTP Basic Auth headers for Jira Cloud.

    Args:
        config: Jira connection config.

    Returns:
        Dict with ``Authorization`` header using Base64-encoded credentials.

    Side Effects:
        None.
    """
    credentials = f"{config.email}:{config.api_token}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def _markdown_to_adf(markdown_text: str) -> dict:
    """Convert a Markdown string to a minimal Atlassian Document Format (ADF) object.

    Purpose:
        Jira Cloud's REST API v3 requires the description field to be in ADF
        (a JSON schema), not plain text or wiki markup.  This function
        produces a simple ADF document with paragraphs and code blocks \u2014
        sufficient to render the VibeCheck report readably inside Jira.

    Inputs:
        markdown_text (str): The report Markdown (may contain headings, lists,
            code blocks, bold text).

    Outputs:
        dict: A valid ADF ``doc`` node.

    Error Behavior:
        Never raises.  Falls back to a single paragraph on parsing error.

    Side Effects:
        None.

    Determinism: Deterministic.
    Idempotency: Yes.
    Thread Safety: Yes.
    Performance: O(n) where n = number of lines.
    """
    # Truncate to stay within Jira's ADF node limit
    if len(markdown_text) > _MAX_DESCRIPTION_CHARS:
        markdown_text = markdown_text[:_MAX_DESCRIPTION_CHARS] + "\n\n[truncated]"

    content_nodes: list[dict] = []
    lines = markdown_text.split("\n")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # Empty paragraph for spacing
            content_nodes.append({"type": "paragraph", "content": []})
            continue

        # Heading
        heading_match = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2)
            content_nodes.append({
                "type": "heading",
                "attrs": {"level": min(level, 6)},
                "content": [{"type": "text", "text": _strip_inline_md(text)}],
            })
            continue

        # Bullet list item
        if stripped.startswith("- "):
            text = stripped[2:]
            # Unwrap checkbox markers
            text = re.sub(r"^\[[ xX]\]\s*", "", text)
            content_nodes.append({
                "type": "bulletList",
                "content": [{
                    "type": "listItem",
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": _strip_inline_md(text)}],
                    }],
                }],
            })
            continue

        # Numbered list item
        numbered_match = re.match(r"^\d+\.\s+(.*)", stripped)
        if numbered_match:
            text = numbered_match.group(1)
            content_nodes.append({
                "type": "orderedList",
                "content": [{
                    "type": "listItem",
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": _strip_inline_md(text)}],
                    }],
                }],
            })
            continue

        # Plain paragraph (with basic bold support)
        text_content = _build_adf_inline_nodes(stripped)
        content_nodes.append({
            "type": "paragraph",
            "content": text_content,
        })

    if not content_nodes:
        content_nodes = [{"type": "paragraph", "content": []}]

    return {
        "type": "doc",
        "version": 1,
        "content": content_nodes,
    }


def _strip_inline_md(text: str) -> str:
    """Remove common inline Markdown markers (bold, italic, code backticks).

    Args:
        text: A single line of Markdown text.

    Returns:
        Plain text with markers stripped.
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"\*(.+?)\*", r"\1", text)       # *italic*
    text = re.sub(r"`(.+?)`", r"\1", text)          # `code`
    return text


def _build_adf_inline_nodes(text: str) -> list[dict]:
    """Convert inline Markdown (bold) to ADF inline text nodes.

    Supports ``**bold**`` markers only.  Other inline markup is stripped.

    Args:
        text: A line of Markdown text.

    Returns:
        List of ADF ``text`` nodes, some with ``strong`` marks.
    """
    nodes: list[dict] = []
    # Split on **bold** markers
    parts = re.split(r"(\*\*.+?\*\*)", text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            inner = part[2:-2]
            nodes.append({
                "type": "text",
                "text": inner,
                "marks": [{"type": "strong"}],
            })
        else:
            # Strip any remaining single-star italics and backtick code
            plain = re.sub(r"\*(.+?)\*", r"\1", part)
            plain = re.sub(r"`(.+?)`", r"\1", plain)
            if plain:
                nodes.append({"type": "text", "text": plain})

    return nodes or [{"type": "text", "text": text}]
