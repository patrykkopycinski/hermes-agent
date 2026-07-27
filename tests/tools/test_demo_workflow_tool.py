"""Tests for tools/demo_workflow_tool.py — capture_demo + record_screen."""

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchemas:
    """Schema definitions must match the expected shape."""

    def test_capture_demo_schema_has_required_fields(self):
        from tools.demo_workflow_tool import CAPTURE_DEMO_SCHEMA

        assert CAPTURE_DEMO_SCHEMA["name"] == "capture_demo"
        props = CAPTURE_DEMO_SCHEMA["parameters"]["properties"]
        assert "url" in props
        assert "steps" in props
        assert "wait_for" in props
        assert CAPTURE_DEMO_SCHEMA["parameters"]["required"] == ["url"]

    def test_record_screen_schema_has_required_fields(self):
        from tools.demo_workflow_tool import RECORD_SCREEN_SCHEMA

        assert RECORD_SCREEN_SCHEMA["name"] == "record_screen"
        props = RECORD_SCREEN_SCHEMA["parameters"]["properties"]
        assert "url" in props
        assert "steps" in props
        assert "duration_seconds" in props
        assert RECORD_SCREEN_SCHEMA["parameters"]["required"] == ["url"]

    def test_schema_descriptions_are_non_empty(self):
        from tools.demo_workflow_tool import CAPTURE_DEMO_SCHEMA, RECORD_SCREEN_SCHEMA

        assert len(CAPTURE_DEMO_SCHEMA["description"]) > 20
        assert len(RECORD_SCREEN_SCHEMA["description"]) > 20


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestExtractScreenshotPath:
    """_extract_screenshot_path should parse various result shapes."""

    def test_extracts_from_dict_with_screenshot_path(self):
        from tools.demo_workflow_tool import _extract_screenshot_path

        result = {"screenshot_path": "/tmp/shot.png"}
        assert _extract_screenshot_path(result) == "/tmp/shot.png"

    def test_extracts_from_json_string(self):
        from tools.demo_workflow_tool import _extract_screenshot_path

        result = json.dumps({"screenshot_path": "/tmp/shot.png"})
        assert _extract_screenshot_path(result) == "/tmp/shot.png"

    def test_extracts_from_dict_with_path_key(self):
        from tools.demo_workflow_tool import _extract_screenshot_path

        result = {"path": "/tmp/alt.png"}
        assert _extract_screenshot_path(result) == "/tmp/alt.png"

    def test_returns_none_on_invalid_json(self):
        from tools.demo_workflow_tool import _extract_screenshot_path

        assert _extract_screenshot_path("not json") is None

    def test_returns_none_on_missing_keys(self):
        from tools.demo_workflow_tool import _extract_screenshot_path

        assert _extract_screenshot_path({"foo": "bar"}) is None

    def test_returns_none_on_non_dict_non_str(self):
        from tools.demo_workflow_tool import _extract_screenshot_path

        assert _extract_screenshot_path(42) is None


class TestBuildSummary:
    """_build_summary should produce readable human text."""

    def test_summary_with_no_errors(self):
        from tools.demo_workflow_tool import _build_summary

        summary = _build_summary("http://localhost:3000", 2, [])
        assert "2 screenshot" in summary
        assert "No console errors" in summary

    def test_summary_with_errors(self):
        from tools.demo_workflow_tool import _build_summary

        summary = _build_summary("http://localhost:3000", 1, ["TypeError"])
        assert "1 screenshot" in summary
        assert "1 console error" in summary


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Tools must self-register with the central registry."""

    def test_capture_demo_registered(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        entry = registry.get_entry("capture_demo")
        assert entry is not None
        assert entry.toolset == "browser"
        assert entry.emoji == "📸"

    def test_record_screen_registered(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        entry = registry.get_entry("record_screen")
        assert entry is not None
        assert entry.toolset == "browser"
        assert entry.emoji == "🎬"

    def test_both_in_browser_toolset(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        browser_tools = registry.get_tool_names_for_toolset("browser")
        assert "capture_demo" in browser_tools
        assert "record_screen" in browser_tools

    def test_schemas_match_registry(self):
        from tools.registry import registry, discover_builtin_tools

        discover_builtin_tools()
        cap_schema = registry.get_schema("capture_demo")
        rec_schema = registry.get_schema("record_screen")
        assert cap_schema["name"] == "capture_demo"
        assert rec_schema["name"] == "record_screen"


# ---------------------------------------------------------------------------
# capture_demo behavior tests (mocked browser)
# ---------------------------------------------------------------------------


class TestCaptureDemoNavigationFailure:
    """When navigation fails, capture_demo should return failure JSON."""

    def test_returns_failure_on_nav_error(self):
        from tools.demo_workflow_tool import capture_demo

        nav_fail = json.dumps({"success": False, "error": "Connection refused"})

        with patch("tools.browser_tool.browser_navigate", return_value=nav_fail):
            result = json.loads(capture_demo(url="http://localhost:9999"))

        assert result["success"] is False
        assert "Navigation failed" in result["error"]
        assert result["url"] == "http://localhost:9999"
        assert "demo_id" in result


class TestCaptureDemoSuccess:
    """When everything works, capture_demo should return screenshots."""

    def test_returns_screenshots_on_success(self):
        from tools.demo_workflow_tool import capture_demo

        nav_ok = json.dumps({"success": True, "url": "http://localhost:3000"})
        vision_ok = json.dumps({"screenshot_path": "/tmp/test_shot.png"})
        console_ok = json.dumps({"messages": []})

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("tools.browser_tool.browser_console", return_value=console_ok),
        ):
            result = json.loads(capture_demo(url="http://localhost:3000", wait_for=0))

        assert result["success"] is True
        assert len(result["screenshots"]) == 1
        assert result["screenshots"][0]["path"] == "/tmp/test_shot.png"
        assert result["console_errors"] == []
        assert "summary" in result
        assert result["media_paths"] == ["/tmp/test_shot.png"]

    def test_multi_step_capture(self):
        from tools.demo_workflow_tool import capture_demo

        nav_ok = json.dumps({"success": True})
        vision_ok = json.dumps({"screenshot_path": "/tmp/step.png"})
        console_ok = json.dumps({"messages": []})

        steps = [
            {"label": "Page loaded", "wait": 0},
            {"label": "After click", "click_ref": "@e1", "wait": 0},
        ]

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("tools.browser_tool.browser_console", return_value=console_ok),
            patch("tools.browser_tool.browser_click", return_value="ok"),
        ):
            result = json.loads(
                capture_demo(url="http://localhost:3000", steps=steps, wait_for=0)
            )

        assert result["success"] is True
        assert len(result["screenshots"]) == 2
        assert result["screenshots"][0]["label"] == "Page loaded"
        assert result["screenshots"][1]["label"] == "After click"

    def test_console_errors_captured(self):
        from tools.demo_workflow_tool import capture_demo

        nav_ok = json.dumps({"success": True})
        vision_ok = json.dumps({"screenshot_path": "/tmp/shot.png"})
        console_errors = json.dumps({
            "messages": [
                {"type": "error", "message": "Uncaught TypeError"},
                {"type": "warn", "message": "Deprecated API"},
            ]
        })

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("tools.browser_tool.browser_console", return_value=console_errors),
        ):
            result = json.loads(capture_demo(url="http://localhost:3000", wait_for=0))

        assert result["success"] is True
        assert "Uncaught TypeError" in result["console_errors"]
        assert "Deprecated API" in result["console_warnings"]


class TestCaptureDemoException:
    """When an unexpected exception occurs, capture_demo should catch it."""

    def test_returns_failure_on_exception(self):
        from tools.demo_workflow_tool import capture_demo

        with patch(
            "tools.browser_tool.browser_navigate",
            side_effect=RuntimeError("Browser crashed"),
        ):
            result = json.loads(capture_demo(url="http://localhost:3000"))

        assert result["success"] is False
        assert "Browser crashed" in result["error"]


class TestCaptureDemoStepWarnings:
    """capture_demo must surface interaction failures instead of silently
    swallowing them (regression: a click that Playwright reports as
    successful but that doesn't trigger the app's state change — e.g.
    EUI tabs — used to produce a screenshot of the wrong page state with
    zero indication anything was off)."""

    def test_click_reported_failure_becomes_step_warning(self):
        from tools.demo_workflow_tool import capture_demo

        nav_ok = json.dumps({"success": True})
        vision_ok = json.dumps({"screenshot_path": "/tmp/step.png"})
        console_ok = json.dumps({"messages": []})
        click_failed = json.dumps({"success": False, "error": "element not found"})

        steps = [{"label": "Click button", "click_ref": "@e1", "wait": 0}]

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("tools.browser_tool.browser_console", return_value=console_ok),
            patch("tools.browser_tool.browser_click", return_value=click_failed),
        ):
            result = json.loads(
                capture_demo(url="http://localhost:3000", steps=steps, wait_for=0)
            )

        assert result["step_warnings"], "expected a step warning for the failed click"
        assert "Click button" in result["step_warnings"][0]
        assert "WARNING" in result["summary"]

    def test_click_raising_exception_becomes_step_warning(self):
        from tools.demo_workflow_tool import capture_demo

        nav_ok = json.dumps({"success": True})
        vision_ok = json.dumps({"screenshot_path": "/tmp/step.png"})
        console_ok = json.dumps({"messages": []})

        steps = [{"label": "Click button", "click_ref": "@e1", "wait": 0}]

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("tools.browser_tool.browser_console", return_value=console_ok),
            patch(
                "tools.browser_tool.browser_click",
                side_effect=RuntimeError("timeout"),
            ),
        ):
            result = json.loads(
                capture_demo(url="http://localhost:3000", steps=steps, wait_for=0)
            )

        assert result["step_warnings"]
        assert "timeout" in result["step_warnings"][0]

    def test_non_json_click_result_is_not_a_false_positive_warning(self):
        """A plain non-JSON string return (e.g. from older/mocked tools)
        must not itself be treated as a failure — only an explicit
        success: False should generate a warning."""
        from tools.demo_workflow_tool import capture_demo

        nav_ok = json.dumps({"success": True})
        vision_ok = json.dumps({"screenshot_path": "/tmp/step.png"})
        console_ok = json.dumps({"messages": []})

        steps = [{"label": "Click button", "click_ref": "@e1", "wait": 0}]

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("tools.browser_tool.browser_console", return_value=console_ok),
            patch("tools.browser_tool.browser_click", return_value="ok"),
        ):
            result = json.loads(
                capture_demo(url="http://localhost:3000", steps=steps, wait_for=0)
            )

        assert result["step_warnings"] == []

    def test_verify_text_missing_flags_step_warning(self):
        """The verify_text assertion must catch the case Playwright can't:
        a click that physically lands but never triggers the app's
        actual state change (e.g. EUI tabs' click handler not firing)."""
        from tools.demo_workflow_tool import capture_demo

        nav_ok = json.dumps({"success": True})
        vision_ok = json.dumps({"screenshot_path": "/tmp/step.png"})
        console_ok = json.dumps({"messages": []})
        click_ok = json.dumps({"success": True})
        # Page text never actually changed after the "successful" click.
        page_text_unchanged = json.dumps({"result": "Overview tab content"})

        steps = [
            {
                "label": "Switch to Proposals tab",
                "click_ref": "@e13",
                "wait": 0,
                "verify_text": "Proposals tab content",
            }
        ]

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("tools.browser_tool.browser_click", return_value=click_ok),
            patch(
                "tools.browser_tool.browser_console",
                side_effect=[console_ok, page_text_unchanged, console_ok],
            ),
        ):
            result = json.loads(
                capture_demo(url="http://localhost:3000", steps=steps, wait_for=0)
            )

        assert result["step_warnings"], "expected a verify_text mismatch warning"
        assert "verify_text" in result["step_warnings"][0]
        assert "Proposals tab content" in result["step_warnings"][0]

    def test_verify_text_present_produces_no_warning(self):
        from tools.demo_workflow_tool import capture_demo

        nav_ok = json.dumps({"success": True})
        vision_ok = json.dumps({"screenshot_path": "/tmp/step.png"})
        console_ok = json.dumps({"messages": []})
        click_ok = json.dumps({"success": True})
        page_text_changed = json.dumps({"result": "Proposals tab content here"})

        steps = [
            {
                "label": "Switch to Proposals tab",
                "click_ref": "@e13",
                "wait": 0,
                "verify_text": "Proposals tab content",
            }
        ]

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("tools.browser_tool.browser_click", return_value=click_ok),
            patch(
                "tools.browser_tool.browser_console",
                side_effect=[console_ok, page_text_changed, console_ok],
            ),
        ):
            result = json.loads(
                capture_demo(url="http://localhost:3000", steps=steps, wait_for=0)
            )

        assert result["step_warnings"] == []


# ---------------------------------------------------------------------------
# record_screen behavior tests (mocked browser)
# ---------------------------------------------------------------------------


class TestRecordScreenFallback:
    """record_screen should work even without ffmpeg."""

    def test_returns_frames_without_ffmpeg(self, tmp_path):
        from tools.demo_workflow_tool import record_screen

        nav_ok = json.dumps({"success": True})
        vision_ok = json.dumps({"screenshot_path": str(tmp_path / "fake.png")})

        # Create the fake screenshot so file copy works
        (tmp_path / "fake.png").write_bytes(b"fake png data")

        with (
            patch("tools.browser_tool.browser_navigate", return_value=nav_ok),
            patch("tools.browser_tool.browser_vision", return_value=vision_ok),
            patch("shutil.which", return_value=None),
        ):  # no ffmpeg
            result = json.loads(
                record_screen(url="http://localhost:3000", duration_seconds=3.0)
            )

        assert result["success"] is True
        assert result["method"] == "screenshots"
        assert result["frame_count"] >= 1
        assert len(result["frames"]) >= 1


class TestRecordScreenError:
    """When navigation fails, record_screen should return failure."""

    def test_navigation_failure_handled(self):
        from tools.demo_workflow_tool import record_screen

        with patch(
            "tools.browser_tool.browser_navigate",
            side_effect=RuntimeError("Connection refused"),
        ):
            result = json.loads(record_screen(url="http://localhost:9999"))

        assert result["success"] is False
        assert "Connection refused" in result["error"]
