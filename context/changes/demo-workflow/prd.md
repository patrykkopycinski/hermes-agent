---
title: Demo Workflow — Product Requirements
change-id: demo-workflow
status: approved
created: 2026-07-25
---

# PRD: Demo Workflow for Hermes Agent

## Problem Statement

Users receiving completed work from Hermes get text descriptions and code
diffs. To verify a feature actually works, they must manually checkout the
branch, start the dev server, and interact with the UI. This friction means
verification is often skipped, and broken features land in production.

Cursor's "Demos and Artifacts" solves this for cloud agents in isolated VMs.
Hermes is local-first and multi-surface (CLI, Telegram, Discord, Desktop), so
the same value applies but must work everywhere — not just in GitHub PRs.

## Target Users

1. **Developers using Hermes for coding tasks** — want to see the working
   result without checking out the branch.
2. **PMs/reviewers receiving agent output** — need quick visual validation
   before approving or merging.
3. **Users on messaging platforms (Telegram, Discord)** — want to see results
   inline without a desktop IDE.

## User Stories

### US1: Post-Feature Screenshot
> As a developer, after Hermes completes a UI feature, I want to see a
> screenshot of the working result so I can verify it without checking out the
> branch.

### US2: Multi-Step Walkthrough
> As a developer, I want Hermes to capture screenshots at each step of a UI
> flow (e.g., "open dialog → fill form → submit → see success state") so I can
> see the full interaction sequence.

### US3: Console Error Detection
> As a developer, I want Hermes to check the browser console for errors after
> navigating, so I know if the feature has runtime issues even if the page
> *looks* fine.

### US4: Video Recording
> As a developer, I want a short video recording of a dynamic UI interaction,
> so I can see animations and transitions that screenshots cannot convey.

### US5: Demo Gallery
> As a desktop app user, I want a visual gallery of captured demos so I can
> browse them without scrolling through chat history.

### US6: Cross-Platform Delivery
> As a Telegram/Discord user, I want captured screenshots delivered inline in
> chat so I can see them on mobile without a desktop.

## Success Criteria

1. After asking Hermes to "show me the result," the user receives at least one
   screenshot within 10 seconds (assuming dev server is running).
2. Console errors are reported alongside screenshots when present.
3. Video recordings work when ffmpeg is available; gracefully degrade to
   individual frames when it is not.
4. The demo gallery pane renders in the desktop app and updates live as demos
   are captured.
5. Screenshots delivered via `MEDIA:<path>` render inline on all surfaces
   (CLI, Telegram, Discord, Desktop).
6. Tools are silently hidden (via `check_fn`) when the browser backend is
   unavailable — no error spam on messaging-only platforms.

## Non-Goals (Phase 2+)

- GitHub PR artifact embedding (needs image hosting + GitHub App integration)
- Remote desktop handoff (VNC infrastructure)
- Agent self-introspection MCP
- Full desktop recording (beyond the browser)
- Automated artifact cleanup (Phase 2: scheduled job)

## Metrics (informal, Phase 1)

- Adoption: skill activates on >50% of UI-feature tasks
- Latency: screenshot captured <10s after dev server is ready
- Reliability: <5% capture failures when browser is available
- Delivery: MEDIA: paths render on 4/4 surfaces
