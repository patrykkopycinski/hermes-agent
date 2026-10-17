# wfgraph Reliability Hardening — Implementation Plan

**Goal:** Make the wfgraph engine safe to actually run: no cross-process run
corruption, no stranded runs on provider faults, no silently-skipped steps, no
unguarded shared state under fan-out.

**Architecture:** All four fixes stay inside `plugins/wfgraph/`. Zero core files
touched (the plugin contract). Liveness reuses the repo's existing no-kill pid
probe rather than hand-rolling one.

**Tech stack:** Python 3.12 venv (`/opt/homebrew/bin/python3.12` — repo caps
`<3.14`, default python3 here is 3.14.7). Gate = `bash scripts/run_tests.sh`.

---

## Phase 1 — Capability audit (read before proposing)

| Problem area | What already handles it | Genuine gap? |
|---|---|---|
| Liveness of a run's owner | `runtime.thread_alive()` — in-process `dict[str, Thread]` | **YES.** Per-process only; any second process reads False for a healthy run. |
| No-kill pid probe | `gateway.status._pid_exists` (verified: True for self, False for 999999) | No — **reuse it**. Repo forbids `os.kill(pid,0)` (Windows bpo-14484 kills the target's console group). |
| Pid reuse discrimination | `hermes_cli.active_sessions._process_start_time` (verified importable) | No — reuse for the lease fingerprint. |
| Atomic file writes | `utils.atomic_write_text`, already used by `store._write_json` | No — reuse. |
| Crash → run marked failed | `runtime.spawn()`'s `try/except` around `advance()` | **YES.** Only wraps the *threaded* path. `background=False` (the durable cron/webhook path) has no guard. |
| Malformed-graph rejection | `WorkflowGraphError` for gate arms (`_run_gate`) | **YES.** Pattern exists but no step-kind validation; unknown kinds are silently dropped. |
| Per-node concurrency | `ThreadPoolExecutor` in `_run_agents` | **YES.** Workers mutate + `save_run()` the shared `state` dict unsynchronised. |
| Dead-run reaping | `runtime.fail_dead_run()` | Partial — correct action, wrong liveness input (see gap 1). |

### Functional requirements

- **FR-001 (MUST)** A run owned by a live process in *another* OS process must
  never be reaped or duplicated by a second process.
- **FR-002 (MUST)** A run whose owner process is genuinely gone must still be
  reaped, so a killed gateway does not wedge a workflow forever.
- **FR-003 (MUST)** An exception from `execute_fn` on the inline path must leave
  the run `failed` on disk, not `running`, and must not propagate raw.
- **FR-004 (MUST)** A step with an unknown `kind` must fail the run loudly at
  start, never report `succeeded` having run nothing.
- **FR-005 (MUST)** Parallel agent nodes must not mutate shared run state from
  pool threads.
- **FR-006 (MUST)** Pid reuse must not make a dead owner look alive.
- **FR-007 (MUST)** All 54 existing tests keep passing.

---

## Task 1: Lease module (FR-001, FR-002, FR-006)

**Files:** Create `plugins/wfgraph/wfgraph/lease.py`, `tests/wfgraph/test_lease.py`

Owner identity = `(pid, process_start_time)` written into the run file under
`owner`. `owner_alive(state)` returns True only when the pid exists AND its
start time matches the fingerprint. Missing `owner` = legacy run = fall back to
`thread_alive` so in-process semantics are preserved.

TDD: test alive-for-self, dead-for-bogus-pid, mismatched-start-time = dead,
absent-owner = falls back.

## Task 2: Wire the lease into start_run (FR-001, FR-002)

**Files:** Modify `runner.start_run`, `runtime.fail_dead_run`

Replace `not thread_alive(runId)` with `not lease.owner_alive(existing)`.
Stamp `owner` in `_fresh_state`. Refresh the stamp when a process adopts a run.

TDD: two-process test — a run owned by a live foreign pid must NOT be reaped;
a run owned by a dead pid MUST be reaped.

## Task 3: Guard the inline path (FR-003)

**Files:** Modify `runner.start_run`

Wrap the `background=False` `advance()` in the same failure handling `spawn()`
uses: mark failed, `save_run`, emit `RunFinished`, re-raise as a controlled
error only after the file is consistent.

TDD: `execute_fn` raises → run file reads `failed`, not `running`.

## Task 4: Reject unknown step kinds (FR-004)

**Files:** Modify `runner.start_run` (validate before first save)

Raise `WorkflowGraphError` listing the offending id + kind and the valid set.

TDD: graph with `kind: "banana"` raises; the five valid kinds still start.

## Task 5: Contain fan-out mutation (FR-005)

**Files:** Modify `runner._compute_agent`, `_run_agents`

Pre-assign session ids on the main thread before submitting; workers receive
only what they read and return results. No `save_run` from a pool thread.

TDD: 8-way fan-out — every node ran, every session id distinct, state intact.

## Task 6: Full gate + E2E (FR-007)

Run `bash scripts/run_tests.sh tests/wfgraph/`; then a real two-process E2E
proving the original corruption scenario is dead.

## Task 7: Mutation-proof the new tests

Revert each fix in a scratch copy and confirm the matching new test goes red.
A test that passes against the unfixed code proves nothing.
