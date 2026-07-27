"""Demo capture and screen recording tools.

Registers two tools:

* ``capture_demo`` — one-shot browser navigation + screenshot + console log
  capture. Wraps the existing browser pipeline so the agent can say "show me
  what the working feature looks like" in a single tool call instead of
  orchestrating browser_navigate → browser_vision → browser_console.

* ``record_screen`` — browser video recording via frame capture. Captures a
  sequence of screenshots at intervals and stitches them into a WebM video
  (if ffmpeg is available), useful for "demos not diffs".

Both tools return artifact paths that work with ``MEDIA:<path>`` delivery in
the desktop app and as file attachments on messaging platforms.
"""

from __future__ import annotations

import json
import logging
import time
import uuid as uuid_mod
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from tools.registry import registry  # noqa: F401 (imported for side effect below)

logger = logging.getLogger(__name__)


def _demos_dir() -> Path:
    """Return (creating) the directory for demo artifacts."""
    from hermes_constants import get_hermes_dir

    return get_hermes_dir("cache/demos", "demo_artifacts")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

CAPTURE_DEMO_SCHEMA: Dict[str, Any] = {
    "name": "capture_demo",
    "description": (
        "Capture a visual demo of a running web application: navigate to a "
        "URL, take screenshots, and collect console logs. Designed for "
        "showcasing completed features ('demos not diffs'). Returns paths to "
        "screenshot files and a console-log summary. Use after completing a "
        "coding task to visually verify and present the result. The returned "
        "screenshot paths can be shared with users via MEDIA:<path>."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "The URL to capture. For local dev servers use "
                    "http://localhost:<port>. The browser must be available."
                ),
            },
            "steps": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Optional list of interaction steps to click through "
                    "before the final screenshot. Each step is a dict with "
                    "an optional 'click_ref' (element ref from snapshot), "
                    "'type_text' (to type into focused element), 'wait' "
                    "(seconds to wait), 'label' (human-readable "
                    "description for the screenshot caption), and "
                    "'verify_text' (optional substring that must appear "
                    "on the page after the step — flags step_warnings if "
                    "missing, since some component libraries, e.g. EUI "
                    "tabs, report a click as successful without the "
                    "underlying React state actually changing)."
                ),
            },
            "wait_for": {
                "type": "number",
                "description": (
                    "Seconds to wait after navigation for the page to "
                    "settle before capturing (default 2.0)."
                ),
            },
            "full_page": {
                "type": "boolean",
                "description": (
                    "If true, capture full-page screenshot (scrolled). "
                    "Default false - captures viewport only."
                ),
            },
        },
        "required": ["url"],
    },
}

RECORD_SCREEN_SCHEMA: Dict[str, Any] = {
    "name": "record_screen",
    "description": (
        "Record a video of a browser interaction - navigate through a web app "
        "while capturing a screen recording. Produces a WebM video file (if "
        "ffmpeg is available) or a sequence of screenshot frames. Use for "
        "dynamic demos showing UI flows, animations, or multi-step "
        "interactions that screenshots cannot convey. The returned video_path "
        "or frame paths can be shared via MEDIA:<path>."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to for the recording.",
            },
            "steps": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "Interaction steps during recording. Each step: "
                    "{'click_ref': '@e5', 'type_text': 'hello', 'wait': 1.5}."
                ),
            },
            "duration_seconds": {
                "type": "number",
                "description": (
                    "Maximum recording duration in seconds (default 15). "
                    "Recording stops early if all steps complete."
                ),
            },
        },
        "required": ["url"],
    },
}


# ---------------------------------------------------------------------------
# Capture demo implementation
# ---------------------------------------------------------------------------


def capture_demo(
    url: str,
    steps: Optional[List[Dict[str, Any]]] = None,
    wait_for: float = 2.0,
    full_page: bool = False,
    task_id: Optional[str] = None,
) -> str:
    """Navigate to a URL, interact with the page, capture screenshots and logs.

    Returns a JSON string with screenshot paths and console log summary.
    """
    from tools.browser_tool import (
        browser_navigate,
        browser_click,
        browser_type,
        browser_scroll,
        browser_vision,
        browser_console,
    )

    demo_id = uuid_mod.uuid4().hex[:12]
    screenshots: List[Dict[str, Any]] = []
    console_errors: List[str] = []
    console_warnings: List[str] = []
    step_warnings: List[str] = []

    try:
        # 1. Navigate to the URL
        nav_result = browser_navigate(url=url, task_id=task_id)
        nav_data = json.loads(nav_result) if isinstance(nav_result, str) else nav_result
        if isinstance(nav_data, dict) and not nav_data.get("success", True):
            return json.dumps({
                "success": False,
                "error": f"Navigation failed: {nav_data.get('error', 'unknown')}",
                "url": url,
                "demo_id": demo_id,
            })

        # Wait for page to settle
        if wait_for > 0:
            time.sleep(wait_for)

        # 2. Capture console logs (before interactions)
        try:
            console_result = browser_console(task_id=task_id)
            if isinstance(console_result, str):
                console_data = json.loads(console_result)
            else:
                console_data = (
                    console_result if isinstance(console_result, dict) else {}
                )
            messages = console_data.get("messages", [])
            console_errors = [
                m.get("message", "")
                for m in messages
                if isinstance(m, dict) and m.get("type") in ("error",)
            ]
            console_warnings = [
                m.get("message", "")
                for m in messages
                if isinstance(m, dict) and m.get("type") in ("warn", "warning")
            ]
        except Exception as e:
            logger.debug("Console capture failed: %s", e)

        # 3. Execute interaction steps
        if steps:
            for i, step in enumerate(steps):
                label = step.get("label", f"Step {i + 1}")
                wait_secs = step.get("wait", 1.0)

                if step.get("click_ref"):
                    try:
                        click_result = browser_click(ref=step["click_ref"], task_id=task_id)
                        click_data = _safe_parse_tool_result(click_result)
                        if isinstance(click_data, dict) and not click_data.get("success", True):
                            step_warnings.append(
                                f"Step {i + 1} ({label}): click on "
                                f"{step['click_ref']} reported failure: "
                                f"{click_data.get('error', 'unknown')}"
                            )
                    except Exception as e:
                        logger.debug("Click failed at step %d: %s", i, e)
                        step_warnings.append(
                            f"Step {i + 1} ({label}): click on "
                            f"{step['click_ref']} raised {e}"
                        )

                if step.get("type_text"):
                    try:
                        type_result = browser_type(
                            ref=step.get("type_ref", step.get("click_ref", "")),
                            text=step["type_text"],
                            task_id=task_id,
                        )
                        type_data = _safe_parse_tool_result(type_result)
                        if isinstance(type_data, dict) and not type_data.get("success", True):
                            step_warnings.append(
                                f"Step {i + 1} ({label}): type reported "
                                f"failure: {type_data.get('error', 'unknown')}"
                            )
                    except Exception as e:
                        logger.debug("Type failed at step %d: %s", i, e)
                        step_warnings.append(
                            f"Step {i + 1} ({label}): type raised {e}"
                        )

                if step.get("scroll_direction"):
                    try:
                        browser_scroll(
                            direction=step["scroll_direction"],
                            task_id=task_id,
                        )
                    except Exception as e:
                        logger.debug("Scroll failed at step %d: %s", i, e)
                        step_warnings.append(
                            f"Step {i + 1} ({label}): scroll raised {e}"
                        )

                if wait_secs > 0:
                    time.sleep(wait_secs)

                # Optional post-step assertion: confirms the interaction
                # actually changed page state, not just that the click
                # landed. Some component libraries (e.g. EUI tabs) report
                # click success without the underlying framework state
                # changing, so a physically-successful click can still
                # leave the page in the wrong state for the screenshot.
                if step.get("verify_text"):
                    try:
                        check_result = browser_console(
                            expression="document.body.innerText",
                            task_id=task_id,
                        )
                        check_data = (
                            json.loads(check_result)
                            if isinstance(check_result, str)
                            else check_result
                        )
                        page_text = (
                            check_data.get("result", "")
                            if isinstance(check_data, dict)
                            else ""
                        )
                        if step["verify_text"] not in str(page_text):
                            step_warnings.append(
                                f"Step {i + 1} ({label}): verify_text "
                                f"'{step['verify_text']}' not found on page "
                                "after the step — the interaction may not "
                                "have actually taken effect (click landed "
                                "but app state didn't change)."
                            )
                    except Exception as e:
                        logger.debug("verify_text check failed at step %d: %s", i, e)
                        step_warnings.append(
                            f"Step {i + 1} ({label}): verify_text check raised {e}"
                        )

                # Capture screenshot at this step
                try:
                    vision_result = browser_vision(
                        question=f"Screenshot of: {label}",
                        task_id=task_id,
                    )
                    screenshot_path = _extract_screenshot_path(vision_result)
                    if screenshot_path:
                        screenshots.append({
                            "path": screenshot_path,
                            "label": label,
                            "step": i + 1,
                        })
                except Exception as e:
                    logger.debug("Screenshot failed at step %d: %s", i, e)
                    step_warnings.append(
                        f"Step {i + 1} ({label}): screenshot capture raised {e}"
                    )

        # 4. Final screenshot (always captured, even with no steps)
        if not screenshots:
            try:
                vision_result = browser_vision(
                    question="Screenshot of the completed feature",
                    task_id=task_id,
                )
                screenshot_path = _extract_screenshot_path(vision_result)
                if screenshot_path:
                    screenshots.append({
                        "path": screenshot_path,
                        "label": "Final result",
                        "step": 1,
                    })
            except Exception as e:
                logger.debug("Final screenshot failed: %s", e)

        # 5. Capture final console state
        try:
            console_result = browser_console(task_id=task_id)
            if isinstance(console_result, str):
                console_data = json.loads(console_result)
            else:
                console_data = (
                    console_result if isinstance(console_result, dict) else {}
                )
            messages = console_data.get("messages", [])
            new_errors = [
                m.get("message", "")
                for m in messages
                if isinstance(m, dict)
                and m.get("type") in ("error",)
                and m.get("message", "") not in console_errors
            ]
            console_errors.extend(new_errors)
        except Exception:
            pass

        # 6. Build result
        result = {
            "success": True,
            "demo_id": demo_id,
            "url": url,
            "screenshots": screenshots,
            "console_errors": console_errors[:20],
            "console_warnings": console_warnings[:10],
            "step_warnings": step_warnings,
            "summary": _build_summary(url, len(screenshots), console_errors, step_warnings),
            "media_paths": [s["path"] for s in screenshots],
        }
        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.exception("capture_demo failed")
        return json.dumps({
            "success": False,
            "error": str(e),
            "url": url,
            "demo_id": demo_id,
        })


def record_screen(
    url: str,
    steps: Optional[List[Dict[str, Any]]] = None,
    duration_seconds: float = 15.0,
    task_id: Optional[str] = None,
) -> str:
    """Record a browser video of a UI interaction flow.

    Returns a JSON string with the video path or frame paths.
    """
    import shutil
    import subprocess
    from tools.browser_tool import browser_navigate, browser_vision, browser_click

    demo_id = uuid_mod.uuid4().hex[:12]
    demos_dir = _demos_dir()
    demos_dir.mkdir(parents=True, exist_ok=True)
    output_dir = demos_dir / f"recording_{demo_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_paths: List[str] = []

    try:
        # Navigate
        browser_navigate(url=url, task_id=task_id)
        time.sleep(2.0)

        # Capture initial frame
        vision_result = browser_vision(question="Recording frame", task_id=task_id)
        path = _extract_screenshot_path(vision_result)
        if path:
            frame = frames_dir / "frame_0000.png"
            shutil.copy2(path, frame)
            frame_paths.append(str(frame))

        # Execute steps with frame capture
        step_idx = 1
        if steps:
            for step in steps:
                if step.get("click_ref"):
                    try:
                        browser_click(ref=step["click_ref"], task_id=task_id)
                    except Exception:
                        pass
                wait = step.get("wait", 1.5)
                time.sleep(min(wait, 3.0))

                vision_result = browser_vision(
                    question="Recording frame", task_id=task_id
                )
                path = _extract_screenshot_path(vision_result)
                if path:
                    frame = frames_dir / f"frame_{step_idx:04d}.png"
                    shutil.copy2(path, frame)
                    frame_paths.append(str(frame))
                    step_idx += 1

                if step_idx >= 20:
                    break

        # Try to create a video with ffmpeg
        video_path = output_dir / f"demo_{demo_id}.webm"
        ffmpeg_bin = shutil.which("ffmpeg")

        if ffmpeg_bin and len(frame_paths) >= 2:
            try:
                framerate = max(1, int(len(frame_paths) / max(duration_seconds, 1)))
                cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-framerate",
                    str(framerate),
                    "-i",
                    str(frames_dir / "frame_%04d.png"),
                    "-c:v",
                    "libvpx",
                    "-pix_fmt",
                    "yuv420p",
                    "-auto-alt-ref",
                    "0",
                    "-vf",
                    "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    str(video_path),
                ]
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0 and video_path.exists():
                    return json.dumps(
                        {
                            "success": True,
                            "demo_id": demo_id,
                            "url": url,
                            "video_path": str(video_path),
                            "frame_count": len(frame_paths),
                            "method": "ffmpeg-slideshow",
                            "media_paths": [str(video_path)],
                            "summary": f"Recorded {len(frame_paths)} frames over ~{duration_seconds:.0f}s.",
                        },
                        ensure_ascii=False,
                    )
                else:
                    logger.debug("ffmpeg failed: %s", result.stderr[:500])
            except Exception as e:
                logger.debug("ffmpeg video creation failed: %s", e)

        # Return frames if video creation failed
        return json.dumps(
            {
                "success": True,
                "demo_id": demo_id,
                "url": url,
                "frames": frame_paths,
                "frame_count": len(frame_paths),
                "method": "screenshots",
                "media_paths": frame_paths[:5],
                "summary": f"Captured {len(frame_paths)} frames (video encoding not available).",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        logger.exception("record_screen failed")
        return json.dumps({
            "success": False,
            "error": str(e),
            "url": url,
            "demo_id": demo_id,
        })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_screenshot_path(
    vision_result: Union[str, Dict[str, Any]],
) -> Optional[str]:
    """Extract the screenshot file path from a browser_vision result."""
    if isinstance(vision_result, str):
        try:
            data = json.loads(vision_result)
        except json.JSONDecodeError:
            return None
    elif isinstance(vision_result, dict):
        data = vision_result
    else:
        return None
    return data.get("screenshot_path") or data.get("path")


def _safe_parse_tool_result(result: Union[str, Dict[str, Any]]) -> Any:
    """Best-effort parse of a browser tool's return value.

    Tool results are usually JSON strings but may be plain non-JSON
    strings (e.g. in tests, or older tool versions). Only treat a
    result as a failure signal when it actually parses to a dict with
    ``success: False`` — an unparseable string is NOT itself evidence
    of failure and must not be reported as a step warning.
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None
    return None


def _build_summary(
    url: str,
    screenshot_count: int,
    errors: List[str],
    step_warnings: Optional[List[str]] = None,
) -> str:
    """Build a human-readable summary of the demo capture."""
    parts = [f"Captured {screenshot_count} screenshot(s) of {url}."]
    if errors:
        parts.append(f"Found {len(errors)} console error(s).")
    else:
        parts.append("No console errors detected.")
    if step_warnings:
        parts.append(
            f"WARNING: {len(step_warnings)} interaction step(s) reported "
            "failures — screenshots may show stale/wrong page state, not "
            "the intended one. Check step_warnings before trusting this demo."
        )
    return " ".join(parts)


def _check_browser_available(**kwargs) -> bool:
    """Check if browser tools are available."""
    try:
        from tools.browser_tool import check_browser_requirements

        return check_browser_requirements()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="capture_demo",
    toolset="browser",
    schema=CAPTURE_DEMO_SCHEMA,
    handler=lambda args, **kw: capture_demo(
        url=args.get("url", ""),
        steps=args.get("steps"),
        wait_for=args.get("wait_for", 2.0),
        full_page=args.get("full_page", False),
        task_id=kw.get("task_id"),
    ),
    check_fn=_check_browser_available,
    emoji="📸",
    description=(
        "Capture a visual demo (screenshots + console logs) of a running web "
        "application in a single tool call."
    ),
)

registry.register(
    name="record_screen",
    toolset="browser",
    schema=RECORD_SCREEN_SCHEMA,
    handler=lambda args, **kw: record_screen(
        url=args.get("url", ""),
        steps=args.get("steps"),
        duration_seconds=args.get("duration_seconds", 15.0),
        task_id=kw.get("task_id"),
    ),
    check_fn=_check_browser_available,
    emoji="🎬",
    description=(
        "Record a video or frame-sequence of a browser interaction flow for "
        "dynamic demos."
    ),
)
