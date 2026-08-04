---
title: Demo Workflow — Shape Notes
feature: demo-workflow
status: shaped
created: 2026-07-25
updated: 2026-07-25
---

# Shape Notes: Demo Workflow for Hermes Agent

## One-Sentence Pitch

After completing a coding task, Hermes automatically captures visual evidence
(screenshots, optionally video) of the working feature and delivers it to the
user — **"demos, not diffs."**

## Problem & Appetite

### Problem
Code changes are verified through diffs and prose. A reviewer reading
"added dark-mode toggle to settings page" cannot *see* the result. The author
must manually screenshot, describe where to click, or ask the reviewer to pull
and run locally. This friction scales badly: every PR, every feature, every
"does it work?" question costs a context switch.

Cursor's "Demos and Artifacts" solves this for *their* cloud-agent-in-a-VM
world. Hermes is local-first and multi-surface (CLI, Telegram, Discord,
Desktop) — the same proof-of-work value applies, but the delivery and capture
paths differ fundamentally.

### Appetite
Phase 1 MVP: 2 tools + 1 skill + 1 desktop plugin. Designed to be built in a
single focused sprint, with deferred pillars cleanly separable.

## Cursor → Hermes Mapping

| Cursor Pillar | Cursor Approach | Hermes Equivalent | Phase 1 Status |
|---|---|---|---|
| Isolated VM | Cloud VM per agent | Terminal backends (Docker/SSH/Daytona/Modal) | Headless — no change |
| Computer use | VM desktop control | `computer-use` skill + 10 browser tools | Already exists |
| Artifact generation | Screenshots/video → PR | `capture_demo` / `record_screen` tools + MEDIA: delivery | **This change** |
| Remote desktop handoff | VNC into agent VM | — | Deferred (Phase 2) |
| Agent self-introspection | Cursor Cloud MCP | Session state (SQLite) | Deferred (Phase 2) |
| PR artifact embedding | Native to GitHub App | `gh` CLI upload | Deferred (Phase 2) |

## Key Design Decisions

### D1: Capture is a first-class tool, not a skill-internal helper
**Decision:** `capture_demo` and `record_screen` are model tools registered via
`registry.register()` in `tools/demo_tool.py`.
**Why:** The agent (and any skill) should be able to call capture directly
without going through the orchestration skill. Keeps the skill thin (a
playbook) and the capture logic reusable.

### D2: Compose existing browser infrastructure — don't fork it
**Decision:** `capture_demo` calls into the existing browser session
(`tools/browser_tool.py`) for navigation and screenshot rather than spinning
up its own Playwright instance.
**Why:** `browser_tool.py` (4946 lines) already manages a persistent browser
context and a stealth-hardened Playwright session. Forking would create a
second browser process, double memory, and desync navigation state. The
existing module-level functions `browser_navigate(url, task_id)` and
`browser_vision(question, annotate, task_id)` are directly importable and
reusable.

### D3: Artifacts are stored on disk, surfaced via MEDIA: protocol
**Decision:** Captured screenshots/videos land in
`~/.hermes/artifacts/demos/<session>/` and are delivered to the user via the
existing `MEDIA:<path>` file-delivery mechanism.
**Why:** MEDIA: already works across all Hermes surfaces (CLI, Telegram,
Discord, Desktop). No new delivery transport needed. Phase 2 can add PR
embedding as a *consumer* of the same artifact store.

### D4: Desktop plugin is read-only gallery — no capture logic in the plugin
**Decision:** The desktop plugin (`~/.hermes/desktop-plugins/demo-gallery/`)
only *renders* artifacts from the store. All capture happens in the Python
agent process.
**Why:** Per AGENTS.md: plugins live in their own directory and work within
the ABCs/hooks we provide. Capture requires browser control (Python-side);
the plugin just needs `host.request()` to read the artifact index.

### D5: Skill is a playbook, not executable code
**Decision:** The `demo-workflow` skill is a SKILL.md that teaches the model
*when* and *how* to capture demos — detect project type, start dev server,
navigate, capture, summarize, deliver.
**Why:** Hermes skills are prompt-embedded playbooks. Keeping orchestration in
the skill (not hardcoded) lets the model adapt to project types it hasn't seen
and skip steps that don't apply.

### D6: Video via frame capture + ffmpeg, not Playwright video API
**Decision:** `record_screen` captures periodic screenshots and stitches them
with ffmpeg, rather than using Playwright's built-in video recording.
**Why:** Playwright's video API requires `record_video_dir` to be set on
browser context *creation* — we'd have to modify the existing browser session
lifecycle in `browser_tool.py` (risky, touches a 4946-line hardened file).
Frame capture is non-invasive and degrades gracefully when ffmpeg is missing.

## Risks & Open Questions

### R1: Dev server lifecycle management
The skill needs to start a dev server (e.g., `npm run dev`), wait for it to be
ready, capture, then shut it down. Dev servers have no standard readiness
signal. **Mitigation:** Poll the port with a timeout; fall back to "capture
what's there" if the server is already running.

### R2: ffmpeg availability
`record_screen` needs ffmpeg for stitching frames into video. **Mitigation:**
`check_fn` probes for ffmpeg; tool gracefully returns individual frames when
absent. ffmpeg is optional, not a hard dependency.

### R3: Artifact bloat
Video files are large. Unbounded capture could fill disk. **Mitigation:**
Default video max duration (30s), configurable; auto-cleanup of artifacts
older than N days via a scheduled job (Phase 1: manual, Phase 2: automated).

### R4: Prompt-cache impact
Adding 2 new tools increases the system prompt. Must keep tool descriptions
tight. **Mitigation:** Tool descriptions under 200 tokens each; the skill
loads on-demand only when the agent identifies a demo-eligible task.

### R5: Browser backend availability on messaging platforms
Telegram/Discord sessions may not have a browser backend. **Mitigation:**
Tools registered to the `browser` toolset; `check_fn` reuses the existing
browser availability probe so tools are silently hidden when the browser is
unavailable.

## Out of Scope (Phase 2+)

- GitHub PR artifact embedding (upload screenshots to PR comments via `gh`)
- Remote desktop handoff (VNC into the agent's environment)
- Agent self-introspection MCP (agent queries its own run state mid-task)
- Automated artifact cleanup cron
- Video editing / trimming UI
- Multi-browser-tab capture (capture across tabs simultaneously)

## References

- Cursor Demos and Artifacts: product announcement / docs
- Hermes browser tools: `tools/browser_tool.py` (4946 lines)
  - `browser_navigate(url, task_id)` at line 2805
  - `browser_vision(question, annotate, task_id)` at line 4030
- Hermes tool registry: `tools/registry.py` — `discover_builtin_tools()` auto-imports
  any `tools/*.py` containing a top-level `registry.register()` call
- Existing tool registration pattern: `tools/memory_tool.py` line 1145
- Hermes plugin SDK: `@hermes/plugin-sdk` (React/Ink, areas: statusBar/panes/palette)
- AGENTS.md: plugin containment rules, prompt-cache constraints
