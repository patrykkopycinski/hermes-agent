"""The ``wfgraph`` tool: author, run, steer and inspect stored agent graphs.

PR #94367 exposed this as a desktop-canvas tool that round-tripped every verb
through the renderer, so the whole surface was unavailable without the GUI.
The graph store is a plain directory under HERMES_HOME, so everything works
headlessly; this registers the headless surface as a normal plugin tool.

The verb set is chosen so an unattended caller can finish what it starts. The
original five (list/read/run/status/cancel) could start a graph and watch it,
but not author one, not answer a human step, not read the event trail, and not
advance a timer -- so a bot could reach ``waiting_human`` or ``waiting_timer``
and had no move left but ``cancel``. Every verb here wraps an engine function
that already exists and is already tested; nothing new happens in this file.
"""
from __future__ import annotations

import json
from typing import Any, Optional

ACTIONS = (
    "list",
    "read",
    "save",
    "delete",
    "run",
    "status",
    "runs",
    "events",
    "respond",
    "tick",
    "cancel",
    "sync",
)

# status/events default page sizes. Event logs grow without bound on a long
# rework loop; an unbounded dump is how a caller burns its context on one call.
_EVENT_LIMIT = 50
_RUNS_LIMIT = 20


def _err(msg: str, **extra: Any) -> str:
    return json.dumps({"error": msg, **extra}, ensure_ascii=False)


def _ok(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _run_view(state: dict, *, events: int = 0) -> dict:
    """The shape a caller polls. Deliberately not the whole run document.

    A run carries every step's session id, full summaries and the entire event
    log; returned verbatim it is tens of kilobytes per poll. This keeps the
    fields a decision can be made from, and ``park`` -- without which a caller
    cannot tell *what* a waiting run is waiting for.
    """
    view = {
        "runId": state.get("runId"),
        "workflow": state.get("workflowId"),
        "status": state.get("status"),
        "ran": state.get("ran"),
        "verdicts": state.get("verdicts"),
        "loops": state.get("loops"),
        "failed": state.get("failed"),
        # The finish record. `status` is the process outcome; the receipt says
        # what that outcome does and does not claim, so a caller polling this
        # does not read "succeeded" as "verified".
        "receipt": state.get("receipt"),
    }
    park = state.get("park")
    if park:
        # What it is blocked on, and therefore which verb unblocks it. The raw
        # park is echoed under `park`, but the two things a caller actually
        # needs are promoted and renamed: `respond` takes a node_id, and a
        # person deciding needs the question. Reporting only "waiting_human"
        # hands back a verb with no arguments for it.
        view["park"] = park
        view["waiting_on"] = {
            "kind": park.get("kind"),
            "node_id": park.get("nodeId"),
            "iteration": park.get("iteration"),
            "prompt": park.get("prompt"),
            "who": park.get("who"),
            "until": park.get("until"),
        }
        view["unblock_with"] = (
            "respond" if park.get("kind") == "human" else "tick"
        )
    if events:
        # The trail is its own jsonl file, not a key on the run document.
        from wfgraph.store import load_events

        log = load_events(str(state.get("runId")))
        view["events"] = log[-events:]
        view["eventsTotal"] = len(log)
    return view


def wfgraph_tool(
    action: str | dict = "",
    workflow: Optional[str] = None,
    run_id: Optional[str] = None,
    payload: Any = None,
    scenario: Optional[dict] = None,
    # Default True: every caller here is a short-lived process (cron job,
    # subagent, shell). Returning a handle to a worker thread that dies
    # with the process strands the run mid-flight with no error anywhere.
    # Pass wait=False deliberately if you have a process that will live.
    wait: bool = True,
    answer: Optional[str] = None,
    node_id: Optional[str] = None,
    note: Optional[str] = None,
    limit: Optional[int] = None,
    events: bool = False,
    **_ignored: Any,  # the dispatcher injects task_id / session context
) -> str:
    # The tool dispatcher calls plugin tools with a single positional dict of
    # all parameters ({"action": ..., "workflow": ...}) rather than keywords.
    # Accept both shapes; explicit keywords win over the packed dict.
    if isinstance(action, dict) and not workflow and not run_id:
        packed = action
        action = packed.get("action") or ""
        workflow = workflow or packed.get("workflow")
        run_id = run_id or packed.get("run_id")
        payload = payload if payload is not None else packed.get("payload")
        scenario = scenario or packed.get("scenario")
        wait = packed.get("wait", wait)
        answer = answer or packed.get("answer")
        node_id = node_id or packed.get("node_id")
        note = note or packed.get("note")
        limit = limit if limit is not None else packed.get("limit")
        events = packed.get("events", events)
    verb = (action or "").strip().lower()
    if verb not in ACTIONS:
        return _err(f"action must be one of: {', '.join(ACTIONS)}.")

    try:
        from wfgraph.store import (
            get_document,
            load_events,
            list_runs,
            load_documents,
            load_run,
            remove_document,
            upsert_document,
        )
        from wfgraph.topology import scenario_of, steps_of
    except Exception as exc:  # pragma: no cover - import guard
        return _err(f"wfgraph engine unavailable: {exc}")

    if verb == "list":
        docs = load_documents().get("docs") or []
        return _ok(
            workflows=[
                {
                    "id": d.get("id"),
                    "name": d.get("name") or d.get("id"),
                    "steps": len(steps_of(scenario_of(d))),
                }
                for d in docs
            ]
        )

    if verb == "read":
        if not (workflow or "").strip():
            return _err("read needs a workflow id or name.")
        doc = get_document(str(workflow).strip())
        if doc is None:
            return _err(f"No workflow called '{workflow}'.")
        return json.dumps(doc, ensure_ascii=False)

    if verb == "save":
        # Authoring headlessly. Without this a bot can only run graphs a human
        # drew in the canvas first, which is the whole of "autonomous use".
        if not (workflow or "").strip():
            return _err("save needs a workflow id.")
        if not isinstance(scenario, dict):
            return _err(
                "save needs a scenario object: "
                '{"steps": [...], "edges": [...]}.'
            )
        wf_id = str(workflow).strip()
        # Validate before storing. A graph that cannot run is worse in the
        # store than rejected at the door: `list` shows it as real, and the
        # failure surfaces later against whoever runs it.
        try:
            from wfgraph.topology import steps_of as _steps_of
            from wfgraph.validate import (
                reject_bad_gate_arms,
                reject_deadlocked_back_edges,
                reject_malformed_structure,
                reject_unknown_kinds,
                reject_unparseable_waits,
                reject_unrouted_gate_arms,
            )

            _steps = _steps_of(scenario)
            reject_malformed_structure(scenario, _steps)
            reject_unknown_kinds(_steps)
            reject_bad_gate_arms(_steps)
            reject_unparseable_waits(_steps)
            reject_unrouted_gate_arms(scenario, _steps)
            reject_deadlocked_back_edges(scenario, _steps)
        except ValueError as exc:
            return _err(f"That graph would not run: {exc}")
        doc = {"id": wf_id, "name": (note or wf_id), "scenario": scenario}
        try:
            upsert_document(doc)
        except Exception as exc:
            return _err(f"Failed to save the workflow: {exc}")
        return _ok(saved=wf_id, steps=len(steps_of(scenario)))

    if verb == "delete":
        if not (workflow or "").strip():
            return _err("delete needs a workflow id.")
        wf_id = str(workflow).strip()
        if get_document(wf_id) is None:
            return _err(f"No workflow called '{wf_id}'.")
        remove_document(wf_id)
        return _ok(deleted=wf_id)

    if verb == "sync":
        # Refresh cron/webhook triggers from the stored documents. A bot that
        # just saved (or deleted) a cron workflow calls this once; the sync
        # creates, updates, and removes the backing no-agent cron jobs.
        try:
            from wfgraph.triggers import sync_triggers
        except Exception as exc:
            return _err(f"Failed to import triggers: {exc}")
        try:
            out = sync_triggers()
        except Exception as exc:
            return _err(f"Failed to sync triggers: {exc}")
        return _ok(
            cron=out.get("cron", []),
            webhooks=out.get("webhooks", {}),
        )

    if verb == "runs":
        # A bot that loses a run id cannot otherwise find its own work.
        wf_filter = (workflow or "").strip() or None
        try:
            found = list_runs(wf_filter)
        except Exception as exc:
            return _err(f"Failed to list runs: {exc}")
        rows = []
        for state in found:
            if not isinstance(state, dict):
                continue
            rows.append(
                {
                    "runId": state.get("runId"),
                    "workflow": state.get("workflowId"),
                    "status": state.get("status"),
                    "startedAt": state.get("startedAt"),
                }
            )
        rows.sort(key=lambda r: str(r.get("startedAt") or ""), reverse=True)
        cap = int(limit or _RUNS_LIMIT)
        return _ok(runs=rows[:cap], total=len(rows))

    if verb == "status":
        if not (run_id or "").strip():
            return _err("status needs a run_id.")
        state = load_run(str(run_id).strip())
        if state is None:
            return _err(f"No run called '{run_id}'.")
        return json.dumps(
            _run_view(state, events=int(limit or _EVENT_LIMIT) if events else 0),
            ensure_ascii=False,
        )

    if verb == "events":
        # Why a run did what it did. `status` gives the final shape only; on a
        # failure the trail is the difference between diagnosing and guessing.
        if not (run_id or "").strip():
            return _err("events needs a run_id.")
        state = load_run(str(run_id).strip())
        if state is None:
            return _err(f"No run called '{run_id}'.")
        log = load_events(str(run_id).strip())
        cap = int(limit or _EVENT_LIMIT)
        return _ok(
            runId=state.get("runId"),
            status=state.get("status"),
            events=log[-cap:],
            total=len(log),
            truncated=len(log) > cap,
        )

    if verb == "respond":
        # Answer a human step. Without this a graph with an approval parks at
        # waiting_human and an unattended caller's only move is to cancel it.
        if not (run_id or "").strip():
            return _err("respond needs a run_id.")
        if answer is None or not str(answer).strip():
            return _err(
                "respond needs an answer: 'approved' or 'denied' "
                "(a denial halts the run, per the step's onFail)."
            )
        state = load_run(str(run_id).strip())
        if state is None:
            return _err(f"No run called '{run_id}'.")
        park = state.get("park") or {}
        if state.get("status") != "waiting_human":
            return _err(
                f"Run '{run_id}' is '{state.get('status')}', not waiting on a "
                "person; there is nothing to answer.",
                status=state.get("status"),
            )
        parked_node = str(park.get("nodeId") or "").strip()
        if not parked_node:
            return _err(
                f"Run '{run_id}' is waiting on a person but its park names no "
                "node, so there is nothing to answer."
            )
        # node_id is optional and only ever a guard. The park is the authority
        # on which step is asking; taking the caller's word for it would let a
        # stale id -- read before the run moved on -- answer a question nobody
        # asked. Passing one that disagrees is a bug worth reporting, not
        # something to silently honour.
        wanted = str(node_id or "").strip()
        if wanted and wanted != parked_node:
            return _err(
                f"Run '{run_id}' is waiting on '{parked_node}', not "
                f"'{wanted}'. Re-read status: the run moved on since you "
                "looked, and answering the wrong step would resolve a "
                "question nobody asked."
            )
        node_id = parked_node
        from wfgraph.runner import respond

        try:
            # The park names the node; the caller does not have to know it.
            respond(str(run_id).strip(), node_id, str(answer).strip(), by=note)
        except ValueError as exc:
            return _err(str(exc))
        after = load_run(str(run_id).strip()) or {}
        return json.dumps(
            {"answered": park.get("nodeId"), **_run_view(after)},
            ensure_ascii=False,
        )

    if verb == "tick":
        # Advance due timer and poll waits. Nothing outside the desktop app
        # called these, so a headless timer wait never resumed at all.
        from wfgraph.waits import tick_polls, tick_timers

        resumed: list[str] = []
        try:
            for fn in (tick_timers, tick_polls):
                got = fn()
                if isinstance(got, (list, tuple)):
                    resumed.extend(str(r) for r in got)
        except Exception as exc:
            return _err(f"Failed to advance waits: {exc}")
        out: dict[str, Any] = {"resumed": resumed, "count": len(resumed)}
        if (run_id or "").strip():
            state = load_run(str(run_id).strip())
            if state is not None:
                out["run"] = _run_view(state)
        return json.dumps(out, ensure_ascii=False)

    if verb == "cancel":
        if not (run_id or "").strip():
            return _err("cancel needs a run_id.")
        from wfgraph.runtime import signal

        signal(str(run_id).strip(), "cancel")
        return _ok(runId=run_id, signalled="cancel")

    # run
    if not (workflow or "").strip():
        return _err("run needs a workflow id or name.")
    try:
        from wfgraph.runner import start_run

        state = start_run(
            str(workflow).strip(),
            scenario=scenario,
            payload=payload,
            source="tool",
            background=not wait,
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"Failed to start the run: {exc}")
    return json.dumps(_run_view(state), ensure_ascii=False)


SCHEMA = {
    "type": "function",
    "function": {
        "name": "wfgraph",
        "description": (
            "Author and run stored agent graphs (workflows): fan-out to parallel "
            "steps, gates that branch on a PASS/FAIL verdict, rework loops, "
            "timer waits and human approvals.\n"
            "Verbs: list, read, save, delete | run, status, runs, events | "
            "respond, tick, cancel.\n"
            "Unattended use: call run with wait=true to execute inline and get "
            "the finished state back. If you must poll, run without wait and "
            "call status with the returned run_id -- status reports 'park' and "
            "'unblock_with' when a run is waiting, telling you whether to call "
            "respond (a human step) or tick (a timer). A run left at "
            "waiting_human or waiting_timer never finishes on its own."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "description": (
                        "list: stored workflows | read: one workflow | save: "
                        "create/replace one | delete: remove one | run: start | "
                        "status: poll one run | runs: recent runs | events: a "
                        "run's trail | respond: answer a human step | tick: "
                        "advance due waits | cancel: stop a run"
                    ),
                },
                "workflow": {
                    "type": "string",
                    "description": "Workflow id (read, save, delete, run; filters runs).",
                },
                "run_id": {
                    "type": "string",
                    "description": "Run id (status, events, respond, tick, cancel).",
                },
                "scenario": {
                    "type": "object",
                    "description": (
                        "The graph, for save (or a one-off run). "
                        '{"steps":[{"id":"a","kind":"agent","config":{"goal":"..."}}],'
                        '"edges":[{"source":"a","target":"b"}]}. '
                        "kind is one of trigger, agent, gate, wait, human. A gate "
                        "arm's id must match the sourceHandle of the edge it takes."
                    ),
                },
                "payload": {"description": "Arbitrary JSON handed to the graph's steps (run)."},
                "wait": {
                    "type": "boolean",
                    "description": (
                        "run: execute inline and return the finished state. "
                        "Prefer this when nothing will poll afterwards."
                    ),
                },
                "answer": {
                    "type": "string",
                    "description": "respond: 'approved' or 'denied'.",
                },
                "note": {
                    "type": "string",
                    "description": "respond: why. save: the workflow's display name.",
                },
                "limit": {
                    "type": "integer",
                    "description": "events/runs: how many to return (newest first).",
                },
                "events": {
                    "type": "boolean",
                    "description": "status: include the tail of the event log.",
                },
            },
            "required": ["action"],
        },
    },
}


def register_tools(ctx) -> None:
    """Deferred-platform pre-registration hook.

    The loader calls this only for manifests it pre-scans before full load;
    the normal path is ``register()`` in ``__init__.py``. Both land on the
    same registry, so this delegates rather than duplicating the schema.
    """
    from . import register

    register(ctx)
