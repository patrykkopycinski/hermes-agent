---
title: Demo Workflow — Technical Specification
change-id: demo-workflow
status: approved
created: 2026-07-25
---

# Technical Spec: Demo Workflow

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Hermes Desktop App                        │
│  ┌───────────────┐  ┌────────────────────────────────────┐  │
│  │  Chat View     │  │  Demo Gallery Plugin (new)        │  │
│  │  MEDIA: paths  │  │  - Thumbnail carousel              │  │
│  │  (existing)    │  │  - Inline video player             │  │
│  │                │  │  - Summary + error count           │  │
│  └───────┬───────┘  └──────────────┬─────────────────────┘  │
│          │                          │ host.request()          │
└──────────┼──────────────────────────┼────────────────────────┘
           │                          │
┌──────────▼──────────────────────────▼────────────────────────┐
│                    Hermes Agent Core (Python)                 │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  Skill: demo-workflow (new)                             │ │
│  │  1. Detect project type (package.json, requirements.txt)│ │
│  │  2. Start dev server (terminal tool)                    │ │
│  │  3. Call capture_demo / record_screen                   │ │
│  │  4. Report errors + deliver MEDIA: paths                │ │
│  └────────────────────────┬────────────────────────────────┘ │
│                           │                                   │
│  ┌────────────────────────▼────────────────────────────────┐ │
│  │  New Tools (tools/demo_tool.py)                         │ │
│  │  ┌─────────────────────┐  ┌──────────────────────────┐ │ │
│  │  │ capture_demo        │  │ record_screen            │ │ │
│  │  │ - navigate          │  │ - periodic screenshots    │ │ │
│  │  │ - screenshot        │  │ - ffmpeg stitch → video   │ │ │
│  │  │ - console capture   │  │ - fallback: frames only   │ │ │
│  │  │ - multi-step        │  │                          │ │ │
│  │  └──────────┬──────────┘  └───────────┬──────────────┘ │ │
│  └─────────────┼─────────────────────────┼────────────────┘ │
│                │                         │                    │
│  ┌─────────────▼─────────────────────────▼────────────────┐  │
│  │  Existing Browser Tools (tools/browser_tool.py)         │  │
│  │  - browser_navigate(url, task_id)        [line 2805]    │  │
│  │  - browser_vision(question, annotate, task_id) [L4030]  │  │
│  │  - browser_console(expression)           [existing]     │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │  Artifact Store (disk)                                  │ │
│  │  ~/.hermes/artifacts/demos/<session_id>/                │ │
│  │    screenshot_001.png                                   │ │
│  │    screenshot_002.png                                   │ │
│  │    video.webm   (or frames/ dir if no ffmpeg)           │ │
│  │    manifest.json   (index of all captures)              │ │
│  └─────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
```

## Components

### 1. `tools/demo_tool.py` (NEW)

Single Python module registering two model tools via `registry.register()`.
Auto-discovered by `discover_builtin_tools()` because it contains a top-level
`registry.register()` call.

#### Tool: `capture_demo`

Orchestrates browser navigation + screenshot + console error capture into one
call. Supports multi-step interaction sequences.

**Schema** (`CAPTURE_DEMO_SCHEMA`):
```python
{
    "name": "capture_demo",
    "description": (
        "Capture a visual demo of a web app: navigate to a URL, take "
        "screenshots, and check the browser console for errors. Returns "
        "paths to screenshots (share via MEDIA:<path>). Supports multi-step "
        "interaction sequences (click, type, wait)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to (e.g. http://localhost:3000)"
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["click", "type", "wait", "screenshot"],
                            "description": "Interaction action"
                        },
                        "selector": {"type": "string", "description": "CSS/ref selector for click/type"},
                        "text": {"type": "string", "description": "Text to type (for 'type' action)"},
                        "wait_ms": {"type": "integer", "default": 500, "description": "Wait duration (for 'wait')"},
                        "label": {"type": "string", "description": "Label for the screenshot (for 'screenshot')"}
                    }
                },
                "description": "Ordered interaction steps. If omitted, just navigates and captures once."
            },
            "wait_for": {
                "type": "string",
                "description": "CSS selector to wait for before capturing (optional). Default: wait for load."
            },
            "full_page": {
                "type": "boolean",
                "default": False,
                "description": "Capture full-page screenshot (not just viewport)"
            }
        },
        "required": ["url"]
    }
}
```

**Handler** signature:
```python
def capture_demo(
    url: str,
    steps: list[dict] | None = None,
    wait_for: str | None = None,
    full_page: bool = False,
    task_id: str | None = None,
) -> str:
    """Returns JSON: {success, screenshots[], console_errors[], media_paths[], summary}"""
```

**Returns** (JSON string):
```json
{
    "success": true,
    "url": "http://localhost:3000",
    "screenshots": [
        {"path": "/Users/.../artifacts/demos/abc123/screenshot_001.png", "label": "Initial load"},
        {"path": "/Users/.../artifacts/demos/abc123/screenshot_002.png", "label": "After click"}
    ],
    "console_errors": ["Uncaught TypeError: ..."],
    "console_warnings": ["Deprecation warning: ..."],
    "media_paths": ["/Users/.../screenshot_001.png", "/Users/.../screenshot_002.png"],
    "summary": "Captured 2 screenshots. 1 console error detected."
}
```

**Implementation notes:**
- Calls `browser_navigate(url, task_id)` from `tools/browser_tool.py`
- Calls `browser_console(expression=None, task_id=...)` to capture console logs
- Extracts screenshot path from `browser_vision` result (parse `screenshot_path` field)
- Writes `manifest.json` into the session artifact directory
- On any browser error, returns `{success: false, error: "..."}` without crashing

#### Tool: `record_screen`

Captures a sequence of screenshots at fixed intervals and stitches them into a
video using ffmpeg (if available). Falls back to returning individual frames.

**Schema** (`RECORD_SCREEN_SCHEMA`):
```python
{
    "name": "record_screen",
    "description": (
        "Record a short video of a web app by capturing periodic screenshots "
        "and stitching them with ffmpeg. If ffmpeg is unavailable, returns "
        "individual PNG frames instead. Useful for capturing animations and "
        "dynamic interactions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to record"},
            "duration_seconds": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 60,
                "description": "Recording duration in seconds (max 60)"
            },
            "fps": {
                "type": "integer",
                "default": 2,
                "minimum": 1,
                "maximum": 10,
                "description": "Frames per second to capture"
            },
            "interact_during": {
                "type": "string",
                "description": "Optional: describe interactions to perform during recording (the model drives these via separate tool calls)"
            }
        },
        "required": ["url"]
    }
}
```

**Handler** signature:
```python
def record_screen(
    url: str,
    duration_seconds: int = 10,
    fps: int = 2,
    task_id: str | None = None,
) -> str:
    """Returns JSON: {success, video_path or frames[], media_paths[], summary}"""
```

**Returns** (JSON string):
```json
{
    "success": true,
    "video_path": "/Users/.../artifacts/demos/abc123/video.webm",
    "frames_captured": 20,
    "duration_seconds": 10,
    "fps": 2,
    "media_paths": ["/Users/.../video.webm"],
    "summary": "Recorded 10s video at 2fps (20 frames)."
}
```

**Fallback** (no ffmpeg):
```json
{
    "success": true,
    "video_path": null,
    "frames": ["/Users/.../frame_001.png", "/Users/.../frame_002.png", ...],
    "frames_captured": 20,
    "note": "ffmpeg not found; returning individual frames.",
    "media_paths": ["/Users/.../frame_001.png", ...],
    "summary": "Captured 20 frames (ffmpeg unavailable for video)."
}
```

#### Helper functions

```python
def _check_browser_and_tools() -> bool:
    """check_fn: returns True if browser backend is available."""

def _check_ffmpeg() -> bool:
    """Returns True if ffmpeg binary is on PATH."""

def _get_session_artifact_dir(session_id: str) -> Path:
    """Returns ~/.hermes/artifacts/demos/<session_id>/, creating it if needed."""

def _extract_screenshot_path(vision_result: str | dict) -> str | None:
    """Parse browser_vision result to extract the screenshot_path field."""

def _write_manifest(session_id: str, entries: list[dict]) -> None:
    """Write/update manifest.json in the session artifact directory."""

def _stitch_video(frames: list[Path], output_path: Path, fps: int) -> bool:
    """Run ffmpeg to stitch PNG frames into a webm video. Returns False on failure."""
```

#### Registration (module level)

```python
from tools.registry import registry

registry.register(
    name="capture_demo",
    toolset="browser",
    schema=CAPTURE_DEMO_SCHEMA,
    handler=lambda args, **kw: capture_demo(
        url=args["url"],
        steps=args.get("steps"),
        wait_for=args.get("wait_for"),
        full_page=args.get("full_page", False),
        task_id=kw.get("task_id"),
    ),
    check_fn=_check_browser_and_tools,
    is_async=False,
    description="Capture screenshots + console errors from a web app",
    emoji="📸",
)

registry.register(
    name="record_screen",
    toolset="browser",
    schema=RECORD_SCREEN_SCHEMA,
    handler=lambda args, **kw: record_screen(
        url=args["url"],
        duration_seconds=args.get("duration_seconds", 10),
        fps=args.get("fps", 2),
        task_id=kw.get("task_id"),
    ),
    check_fn=lambda: _check_browser_and_tools(),
    is_async=False,
    description="Record a short video of a web app (frame capture + ffmpeg)",
    emoji="🎬",
)
```

### 2. `~/.hermes/skills/software-development/demo-workflow/SKILL.md` (NEW)

Standard SKILL.md with YAML frontmatter. Teaches the model *when* and *how* to
capture demos. Not executable code — a prompt-embedded playbook.

**Frontmatter:**
```yaml
---
name: demo-workflow
description: >-
  Capture visual demos (screenshots, video) of completed UI work and deliver
  them via MEDIA: paths. Use after completing a UI feature, when the user asks
  to "show me the result", or when reviewing a PR with visual changes.
trigger_phrases:
  - "show me the result"
  - "demo"
  - "screenshot"
  - "record"
  - "showcase"
  - "does it work"
  - "visual"
required_tools:
  - capture_demo
  - record_screen
  - browser_navigate
  - terminal
---
```

**Body sections:**
1. **When to use** — after UI feature completion, on "show me" requests, during PR review
2. **5-step process:**
   - Step 1: Detect project type (check `package.json`, `requirements.txt`, `Cargo.toml`, etc.)
   - Step 2: Start dev server (via `terminal` tool: `npm run dev` / `python -m http.server` / etc.)
   - Step 3: Wait for server readiness (poll `http://localhost:<port>` with timeout)
   - Step 4: Call `capture_demo(url=...)` — check returned console_errors
   - Step 5: Deliver — include `MEDIA:<path>` in response; summarize what the screenshots show
3. **Multi-step demos** — use the `steps` parameter for interaction sequences
4. **Video demos** — use `record_screen` for animations/transitions
5. **Error reporting** — always surface console errors, even if the page looks fine
6. **Non-goals** — don't capture if no browser; don't start servers the user didn't ask about

### 3. `~/.hermes/desktop-plugins/demo-gallery/plugin.js` (NEW)

ESM plugin using `@hermes/plugin-sdk`. Read-only gallery — renders artifacts
from the store. No capture logic.

**Structure:**
```javascript
import { React, useState, useEffect } from '@hermes/plugin-sdk';

export default function demoGalleryPlugin({ host, ctx }) {
    // Register a bottom pane
    ctx.register({
        area: 'panes',
        placement: 'bottom',
        height: 280,
        id: 'demo-gallery',
        label: 'Demo Gallery',
        render: () => <DemoGallery host={host} />
    });

    // Register palette command
    ctx.register({
        area: 'palette',
        command: 'toggle-demo-gallery',
        label: 'Toggle Demo Gallery',
        shortcut: 'cmd+shift+d',
        action: () => host.emit('demo-gallery:toggle')
    });
}

function DemoGallery({ host }) {
    const [demos, setDemos] = useState([]);
    const [selected, setSelected] = useState(null);

    useEffect(() => {
        // Listen for new captures
        host.on('tool_result', (event) => {
            if (event.tool === 'capture_demo' || event.tool === 'record_screen') {
                refreshDemos();
            }
        });
        refreshDemos();
    }, []);

    async function refreshDemos() {
        const result = await host.request('demo.list');
        if (result.ok) setDemos(result.demos);
    }

    // Render: thumbnail strip (left) + main preview (center) + summary (right)
    return (
        <Box flexDirection="row">
            <ThumbnailStrip demos={demos} onSelect={setSelected} />
            <PreviewArea demo={selected} />
            <SummaryPanel demo={selected} />
        </Box>
    );
}
```

**Plugin capabilities:**
- Bottom pane (`placement: 'bottom'`, `height: 280px`)
- Thumbnail carousel of captured screenshots
- Inline video player for `record_screen` output
- Summary panel: timestamp, URL, console error count
- Palette command "Toggle Demo Gallery" (Cmd+Shift+D)
- Live update when `capture_demo` / `record_screen` tool results arrive

## Data Flow

1. Agent completes a coding task (e.g., "add dark-mode toggle to settings page")
2. Skill activates (trigger phrases or model judgment)
3. Skill detects project type → starts dev server via `terminal` tool
4. Skill calls `capture_demo(url="http://localhost:3000", steps=[...])`
5. `capture_demo` internally:
   - Calls `browser_navigate(url, task_id)` → page loads
   - Executes interaction steps (click/type/wait) via browser tool functions
   - Calls `browser_vision(question="screenshot", task_id)` → captures screenshot
   - Calls `browser_console(expression=None, task_id)` → captures console logs
   - Saves screenshots to `~/.hermes/artifacts/demos/<session>/`
   - Returns JSON with paths + console errors
6. Agent includes `MEDIA:<path>` in response text
7. Desktop app renders inline in chat + gallery pane updates
8. Telegram/Discord deliver the image inline

## Integration Points

- **Tool registry:** `tools/demo_tool.py` auto-discovered by
  `discover_builtin_tools()` in `tools/registry.py` — scans `tools/*.py` for
  top-level `registry.register()` calls (AST-checked, not just text match).
- **Toolset:** Both tools join the existing `browser` toolset (shares browser
  backend availability check).
- **Skill loader:** Standard SKILL.md format in `~/.hermes/skills/`.
- **Desktop plugin loader:** Standard `plugin.js` in
  `~/.hermes/desktop-plugins/demo-gallery/` — hot-reloaded at runtime.
- **MEDIA: protocol:** Existing file-delivery mechanism — no changes needed.

## Error Handling

| Failure | Behavior |
|---|---|
| Browser not available | `check_fn` returns False → tools hidden from schema entirely |
| Navigation fails (bad URL, timeout) | Returns `{success: false, error: "..."}` |
| Console capture fails | Logged; continues with screenshots only |
| ffmpeg not found | `record_screen` returns individual frames with a note |
| No screenshots captured | Returns `{success: false, error: "No screenshots captured"}` |
| Artifact dir not writable | Returns `{success: false, error: "Cannot write to artifact dir: ..."}` |
| `browser_vision` returns no path | Logged; step skipped; continues with remaining steps |

## Dependencies

- **Playwright:** Already shipped with Hermes (browser tools). No new install.
- **ffmpeg:** Optional. Probed at runtime via `shutil.which("ffmpeg")`.
  If absent, `record_screen` degrades to frame output.
- **@hermes/plugin-sdk:** Already available in the desktop app.

## Constraints (from AGENTS.md)

- **Prompt-cache sacred:** Tool descriptions kept tight (<200 tokens each).
  The skill is not in the base system prompt — loads on-demand.
- **Core narrow:** No changes to `tools/registry.py`, `model_tools.py`, or
  `run_agent.py`. The new tool file is purely additive.
- **Plugin containment:** The desktop plugin calls `host.request()` only —
  no direct filesystem or browser access from JavaScript.
