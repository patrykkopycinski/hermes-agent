---
title: Demo Workflow — Implementation Plan
change-id: demo-workflow
status: completed
created: 2026-07-25
updated: 2026-07-25
---

# Implementation Plan: Demo Workflow

## Overview

Four phases: core capture tools, GitHub PR integration, remote desktop handoff, and agent self-introspection.

---

## Phase 1: Core Tool — `capture_demo` + `record_screen`

**Files:**
- `tools/demo_workflow_tool.py` (new)

**Success Criteria:**
- [x] `tools/demo_workflow_tool.py` exists with `registry.register()` calls
- [x] Python import succeeds
- [x] Tools appear in registry (74 → 76 total)
- [x] No lint errors
- [x] Functional test: capture_demo returns screenshot (17.5KB PNG)
- [x] Functional test: record_screen produces WebM video via ffmpeg (13.5KB)
- [x] Multi-step capture: 3 labeled screenshots captured

---

## Phase 2: GitHub PR Integration — `attach_demo_to_pr`

**Files:**
- `tools/pr_demo_tool.py` (new)
- `tests/tools/test_demo_workflow_phase234.py` (new)

**Success Criteria:**
- [x] Tool registered in `github` toolset
- [x] URL parser handles standard/trailing-slash/hyphenated/fragment URLs
- [x] Image upload to freeimage.host works (returns direct image URL)
- [x] PR comment posted via `gh` CLI with embedded media
- [x] E2E test: screenshot uploaded and comment posted on real PR (hermes-demo-test#1)
- [x] Fallback upload to tmpfiles.org, catbox.moe

---

## Phase 3: Remote Desktop Handoff — `remote_desktop`

**Files:**
- `tools/remote_desktop_tool.py` (new)

**Success Criteria:**
- [x] Tool registered in `terminal` toolset
- [x] macOS backend: Screen Sharing via launchctl plist
- [x] Docker backend: noVNC container with web viewer
- [x] SSH tunnel backend: for remote hosts
- [x] Start/status/stop lifecycle tested
- [x] Availability check detects backend automatically

---

## Phase 4: Agent Self-Introspection — `session_context` + `run_summary`

**Files:**
- `tools/introspection_tool.py` (new)

**Success Criteria:**
- [x] Both tools registered in `skills` toolset
- [x] session_context returns: cwd, git diff, loaded skills (205 found)
- [x] run_summary produces markdown with changed files + suggested demo targets
- [x] run_summary JSON format works
- [x] Focus parameter filters relevant files

---

## Phase 5: Skill + Desktop Plugin

**Files:**
- `~/.hermes/skills/software-development/demo-workflow/SKILL.md`
- `~/.hermes/desktop-plugins/demo-gallery/plugin.js`

**Success Criteria:**
- [x] SKILL.md with correct frontmatter and trigger phrases
- [x] Plugin.js registers bottom pane + palette command
- [x] Node.js syntax check passes

---

## Phase 6: Test Suite

**Files:**
- `tests/tools/test_demo_workflow_tool.py` (22 tests)
- `tests/tools/test_demo_workflow_phase234.py` (26 tests)

**Success Criteria:**
- [x] 48/48 tests pass
- [x] Ruff lint: All checks passed
- [x] Ruff format: All files formatted

---

## Progress

- [x] Phase 1: Core Tool — `capture_demo` + `record_screen`
- [x] Phase 2: GitHub PR Integration — `attach_demo_to_pr`
- [x] Phase 3: Remote Desktop Handoff — `remote_desktop`
- [x] Phase 4: Agent Self-Introspection — `session_context` + `run_summary`
- [x] Phase 5: Skill + Desktop Plugin
- [x] Phase 6: Test Suite (48 tests passing)
