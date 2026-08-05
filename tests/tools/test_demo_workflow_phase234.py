"""Tests for Phase 2-4 demo-workflow tools."""

import json
import os
import shutil
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Phase 2: attach_demo_to_pr
# ---------------------------------------------------------------------------


class TestParsePRUrl:
    def test_standard_url(self):
        from tools.pr_demo_tool import _parse_pr_url

        owner, repo, num = _parse_pr_url("https://github.com/owner/repo/pull/123")
        assert owner == "owner"
        assert repo == "repo"
        assert num == "123"

    def test_url_with_trailing_slash(self):
        from tools.pr_demo_tool import _parse_pr_url

        owner, repo, num = _parse_pr_url("https://github.com/owner/repo/pull/123/")
        assert owner == "owner"
        assert repo == "repo"
        assert num == "123"

    def test_url_with_hyphenated_repo(self):
        from tools.pr_demo_tool import _parse_pr_url

        owner, repo, num = _parse_pr_url(
            "https://github.com/patrykk/hermes-demo-test/pull/1"
        )
        assert owner == "patrykk"
        assert repo == "hermes-demo-test"
        assert num == "1"

    def test_url_with_fragment(self):
        from tools.pr_demo_tool import _parse_pr_url

        owner, repo, num = _parse_pr_url(
            "https://github.com/o/r/pull/5#issuecomment-999"
        )
        assert owner == "o"
        assert repo == "r"
        assert num == "5"

    def test_invalid_url_raises(self):
        from tools.pr_demo_tool import _parse_pr_url

        with pytest.raises(ValueError):
            _parse_pr_url("not a url")

    def test_non_github_url_raises(self):
        from tools.pr_demo_tool import _parse_pr_url

        with pytest.raises(ValueError):
            _parse_pr_url("https://gitlab.com/owner/repo/pull/1")


class TestFormatPRComment:
    def test_basic_formatting(self):
        from tools.pr_demo_tool import _format_pr_comment

        body = _format_pr_comment(
            "📸 Test",
            "Summary text",
            [{"url": "https://example.com/img.png", "label": "Shot", "type": "image"}],
        )
        assert "## 📸 Test" in body
        assert "Summary text" in body
        assert "![Shot](https://example.com/img.png)" in body

    def test_video_formatting(self):
        from tools.pr_demo_tool import _format_pr_comment

        body = _format_pr_comment(
            "Demo",
            "",
            [
                {
                    "url": "https://example.com/video.webm",
                    "label": "Recording",
                    "type": "video",
                }
            ],
        )
        assert "[🎬 Recording]" in body

    def test_console_errors_section(self):
        from tools.pr_demo_tool import _format_pr_comment

        body = _format_pr_comment(
            "Demo", "", [], console_errors=["TypeError: x is undefined"]
        )
        assert "TypeError" in body
        assert "Console Errors" in body


class TestAttachDemoToPR:
    def test_invalid_url_returns_error(self):
        from tools.pr_demo_tool import attach_demo_to_pr

        result = json.loads(
            attach_demo_to_pr(
                pr_url="not-a-url",
                screenshots=["/tmp/fake.png"],
            )
        )
        assert result["success"] is False

    def test_all_uploads_failed(self):
        from tools.pr_demo_tool import attach_demo_to_pr

        with patch("tools.pr_demo_tool._upload_media", return_value=None):
            result = json.loads(
                attach_demo_to_pr(
                    pr_url="https://github.com/o/r/pull/1",
                    screenshots=["/tmp/nonexistent.png"],
                )
            )
        assert result["success"] is False
        assert "failed" in result["error"].lower()

    def test_successful_comment_post(self):
        from tools.pr_demo_tool import attach_demo_to_pr

        with (
            patch(
                "tools.pr_demo_tool._upload_media",
                return_value="https://example.com/img.png",
            ),
            patch(
                "tools.pr_demo_tool._post_pr_comment",
                return_value="https://github.com/o/r/pull/1#issuecomment-1",
            ),
        ):
            result = json.loads(
                attach_demo_to_pr(
                    pr_url="https://github.com/o/r/pull/1",
                    screenshots=["/tmp/fake.png"],
                    summary="Test summary",
                )
            )
        assert result["success"] is True
        assert result["media_count"] == 1


# ---------------------------------------------------------------------------
# Phase 3: remote_desktop
# ---------------------------------------------------------------------------


class TestRemoteDesktopStatus:
    def test_status_no_session(self):
        from tools.remote_desktop_tool import remote_desktop, _active_sessions

        _active_sessions.clear()
        result = json.loads(remote_desktop(action="status"))
        assert result["active"] is False

    def test_stop_no_session(self):
        from tools.remote_desktop_tool import remote_desktop, _active_sessions

        _active_sessions.clear()
        result = json.loads(remote_desktop(action="stop"))
        assert result["success"] is True

    def test_unknown_action(self):
        from tools.remote_desktop_tool import remote_desktop

        result = json.loads(remote_desktop(action="bogus"))
        assert result["success"] is False


class TestRemoteDesktopStartStop:
    def test_macos_start_and_stop(self):
        from tools.remote_desktop_tool import remote_desktop, _active_sessions

        _active_sessions.clear()

        # Mock macOS backend
        mock_result = {"success": True, "backend": "mock-vnc", "vnc_port": 5900}
        with patch(
            "tools.remote_desktop_tool._start_vnc_macos", return_value=mock_result
        ):
            r = json.loads(remote_desktop(action="start", host="localhost"))
        assert r["success"] is True
        assert r["backend"] == "mock-vnc"

        # Status should show active
        r2 = json.loads(remote_desktop(action="status"))
        assert r2["active"] is True

        # Stop
        r3 = json.loads(remote_desktop(action="stop"))
        assert r3["success"] is True

        # Status should show inactive
        r4 = json.loads(remote_desktop(action="status"))
        assert r4["active"] is False


# ---------------------------------------------------------------------------
# Phase 4: session_context + run_summary
# ---------------------------------------------------------------------------


class TestSessionContext:
    def test_returns_cwd(self):
        from tools.introspection_tool import session_context

        result = json.loads(session_context())
        assert "session" in result
        assert "cwd" in result["session"]

    def test_returns_skills_list(self):
        from tools.introspection_tool import session_context

        result = json.loads(session_context(include_skills=True))
        assert isinstance(result.get("skills", []), list)

    def test_includes_git_info(self):
        from tools.introspection_tool import session_context

        result = json.loads(session_context(include_diff=True))
        assert "git" in result
        assert "has_changes" in result["git"]


class TestRunSummary:
    def test_markdown_format(self):
        from tools.introspection_tool import run_summary

        summary = run_summary(format="markdown")
        assert "# Run Summary" in summary

    def test_json_format(self):
        from tools.introspection_tool import run_summary

        summary = run_summary(format="json")
        data = json.loads(summary)
        assert "session" in data
        assert "changes" in data

    def test_focus_included(self):
        from tools.introspection_tool import run_summary

        summary = run_summary(format="markdown", focus="frontend")
        assert "frontend" in summary.lower()


# ---------------------------------------------------------------------------
# Registration tests for all new tools
# ---------------------------------------------------------------------------


class TestPhase234Registration:
    def test_attach_demo_to_pr_registered(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        entry = registry.get_entry("attach_demo_to_pr")
        assert entry is not None
        assert entry.toolset == "github"

    def test_remote_desktop_registered(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        entry = registry.get_entry("remote_desktop")
        assert entry is not None
        assert entry.toolset == "terminal"

    def test_session_context_registered(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        entry = registry.get_entry("session_context")
        assert entry is not None
        assert entry.toolset == "skills"

    def test_run_summary_registered(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        entry = registry.get_entry("run_summary")
        assert entry is not None
        assert entry.toolset == "skills"
