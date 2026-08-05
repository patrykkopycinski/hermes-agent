---
change-id: demo-workflow
title: Demo Workflow — Visual Evidence Capture
status: completed
created: 2026-07-25
updated: 2026-07-25
---

# Change: Demo Workflow

## Summary

Reimplementation of Cursor's "Demos and Artifacts" feature for Hermes Agent. Six new tools, one skill, one desktop plugin.

## Components Delivered

| Phase | Component | Tool Name | Toolset | Tests |
|---|---|---|---|---|
| 1 | Screenshot capture | `capture_demo` | browser | 22 |
| 1 | Video recording | `record_screen` | browser | |
| 2 | GitHub PR attachment | `attach_demo_to_pr` | github | 9 |
| 3 | Remote desktop (VNC) | `remote_desktop` | terminal | 4 |
| 4 | Session introspection | `session_context` | skills | 3 |
| 4 | Run summary | `run_summary` | skills | 3 |
| 5 | Orchestration skill | `demo-workflow` (SKILL.md) | — | — |
| 5 | Desktop gallery UI | `demo-gallery` (plugin.js) | — | — |

## Verification Results

- 48/48 tests pass (6.64s)
- Ruff lint: clean
- Ruff format: clean
- 6 new tools registered (76 → 78 total)
- Live E2E: screenshot captured, uploaded, posted to GitHub PR
- Live E2E: WebM video recorded via ffmpeg
- Live E2E: session_context reports 205 loaded skills
