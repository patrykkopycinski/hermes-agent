---
name: wfgraph-workflows
description: Use when a bot runs or authors a wfgraph workflow. Drives graphs unattended via the wfgraph tool.
---

# Running wfgraph workflows unattended

A workflow is a stored graph of steps (`agent`, `gate`, `human`, `wait`,
`trigger`) plus edges. The `wfgraph` tool is the whole surface: one verb per
call, JSON string back. Parse it; on failure the reply is `{"error": ...}`.

## The loop that matters

```
run  -> status -> (parked? unblock it) -> status -> ... -> receipt
```

`run` is synchronous by default and returns when the run finishes **or
parks**. A parked run is not a failure; it is waiting for something only you
can supply.

## Reading a parked run

`status` on a parked run carries `unblock_with` and `waiting_on`. Never guess
the verb or the node -- they are both in the reply:

```python
st = call(action="status", run_id=rid)
if st["status"] in ("waiting_human", "waiting_world"):
    verb = st["unblock_with"]           # "respond" or "tick"
    node = st["waiting_on"]["node_id"]  # which step is asking
    ask  = st["waiting_on"]["prompt"]   # what it wants to know
```

| status | means | unblock with |
|---|---|---|
| `waiting_human` | a person must decide | `respond` |
| `waiting_world` | a timer/poll must come due | `tick` |
| `succeeded` / `failed` / `cancelled` | terminal, has a receipt | nothing |

## Answering a human step

```python
call(action="respond", run_id=rid, answer="approved")   # or "denied"
```

A denial halts the run per the step's `onFail`; that is a real outcome with
a receipt, not an error. Pass `node_id` only as a guard -- if it disagrees
with the park the call is refused, which is what you want when your status
read is stale.

## Timer and poll waits

A wait step's config is `{"until": {"type": "timer", "spec": "1h"}}` -- spec
is a plain string like `30s`, `5m`, `1h`, `2d`, not a nested object.

The process that starts a wait arms an in-process timer. **If that process
exits, nothing resumes the park.** For cron and subagent callers that is the
normal case, so a due run needs an explicit tick:

```python
call(action="tick", run_id=rid)   # -> {"resumed": [...], "count": n}
```

`tick` with no `run_id` sweeps every run -- the right call for a cron job
that owns the whole store.

## Never trust `status` alone for "did it work"

`succeeded` is a *process* outcome: the graph reached its end without
crashing. It does not mean the work was verified. Read the receipt:

```python
r = call(action="status", run_id=rid)["receipt"]
# {"state","finishedAt","durationMs","nodesRan","evidence","verified","meaning"}
```

`evidence: false` or `verified: false` means nothing checked the claim, and
`meaning` says so in words. Report that, do not upgrade it to success.

## Diagnosing

```python
call(action="events", run_id=rid, limit=50)   # ordered trail, newest last
call(action="runs", workflow="my-wf")         # find a run you lost the id of
```

## Scheduled and webhook triggers

After any save/delete of a triggered workflow, bots call `wfgraph` with
`action="sync"` to create/update/remove the backing cron jobs.

A workflow's FIRST trigger step may declare a start condition. The key is
`on` (not `trigger`):

```python
{"id": "t", "kind": "trigger", "config": {"on": {"type": "cron", "spec": "*/5 * * * *"}}}
```

After saving, call `sync_triggers()` from `wfgraph.triggers` (the tool does not
expose a sync verb yet) — this creates a real no-agent cron job per cron
trigger and removes jobs for deleted workflows. The generated script fires
`start_from_trigger` in a fresh process, so runs survive process exit.
Cron-expression schedules need `pip install croniter`.

## Authoring a graph

```python
call(action="save", workflow="nightly", scenario={
  "steps": [
    {"id": "t",   "kind": "trigger", "config": {}},
    {"id": "chk", "kind": "agent",   "config": {"goal": "check X"}},
    {"id": "g",   "kind": "gate",    "config": {"arms": [
        {"id": "pass", "when": {"mode": "all-pass"}},
        {"id": "fail", "when": {"mode": "any-fail"}}]}},
    {"id": "ship", "kind": "agent",  "config": {"goal": "ship"}},
    {"id": "fix",  "kind": "agent",  "config": {"goal": "fix"}},
  ],
  "edges": [
    {"id": "e1", "source": "t",   "target": "chk"},
    {"id": "e2", "source": "chk", "target": "g"},
    {"id": "e3", "source": "g",   "target": "ship", "sourceHandle": "pass"},
    {"id": "e4", "source": "g",   "target": "fix",  "sourceHandle": "fail"},
  ]})
```

**Every gate arm id must match a `sourceHandle` on an outgoing edge.** A
mismatch is refused at `save`/`run` time now, but the failure it prevents is
a gate that decides and has nowhere to go.

A rework edge that points backwards must carry `"loop": true`, or the
target is treated as an unmet dependency and never becomes ready.

## Verdicts

An agent step's verdict is read from a **line that is the verdict** --
`PASS`, `FAIL`, `Result: PASS`, or `FAIL: reason` -- scanned bottom-up, or
from a JSON `verdict` field. Prose containing the word "pass" is not a
verdict. When writing an agent step's goal, ask for that final line
explicitly.
