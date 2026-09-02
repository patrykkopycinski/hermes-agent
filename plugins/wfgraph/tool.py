"""The ``wfgraph`` tool: list, inspect, start and steer stored agent graphs.

PR #94367 exposed this as a desktop-canvas tool that round-tripped every verb
through the renderer, so the whole surface was unavailable without the GUI.
The graph store is a plain directory under HERMES_HOME, so everything except
authoring-by-drag works headlessly; this registers the headless half as a
normal plugin tool and leaves the canvas out.
"""
from __future__ import annotations

import json
from typing import Any, Optional

ACTIONS = ("list", "read", "run", "status", "cancel")


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


def wfgraph_tool(
    action: str = "",
    workflow: Optional[str] = None,
    run_id: Optional[str] = None,
    payload: Any = None,
    scenario: Optional[dict] = None,
    wait: bool = False,
) -> str:
    verb = (action or "").strip().lower()
    if verb not in ACTIONS:
        return _err(f"action must be one of: {', '.join(ACTIONS)}.")

    try:
        from wfgraph.store import get_document, load_documents, load_run
        from wfgraph.topology import scenario_of, steps_of
    except Exception as exc:  # pragma: no cover - import guard
        return _err(f"wfgraph engine unavailable: {exc}")

    if verb == "list":
        docs = load_documents().get("docs") or []
        return json.dumps(
            {
                "workflows": [
                    {
                        "id": d.get("id"),
                        "name": d.get("name") or d.get("id"),
                        "steps": len(steps_of(scenario_of(d))),
                    }
                    for d in docs
                ]
            },
            ensure_ascii=False,
        )

    if verb == "read":
        if not (workflow or "").strip():
            return _err("read needs a workflow id or name.")
        doc = get_document(str(workflow).strip())
        if doc is None:
            return _err(f"No workflow called '{workflow}'.")
        return json.dumps(doc, ensure_ascii=False)

    if verb == "status":
        if not (run_id or "").strip():
            return _err("status needs a run_id.")
        state = load_run(str(run_id).strip())
        if state is None:
            return _err(f"No run called '{run_id}'.")
        return json.dumps(
            {
                "runId": state.get("runId"),
                "workflow": state.get("workflowId"),
                "status": state.get("status"),
                "ran": state.get("ran"),
                "verdicts": state.get("verdicts"),
                "loops": state.get("loops"),
                "failed": state.get("failed"),
            },
            ensure_ascii=False,
        )

    if verb == "cancel":
        if not (run_id or "").strip():
            return _err("cancel needs a run_id.")
        from wfgraph.runtime import signal

        signal(str(run_id).strip(), "cancel")
        return json.dumps({"runId": run_id, "signalled": "cancel"}, ensure_ascii=False)

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
    return json.dumps(
        {
            "runId": state.get("runId"),
            "status": state.get("status"),
            "workflow": state.get("workflowId"),
            "ran": state.get("ran"),
            "verdicts": state.get("verdicts"),
        },
        ensure_ascii=False,
    )


SCHEMA = {
    "type": "function",
    "function": {
        "name": "wfgraph",
        "description": (
            "Run and inspect stored agent graphs (workflows): fan-out to parallel "
            "steps, gates that branch on a PASS/FAIL verdict, and rework loops. "
            "Call action='list' first to see what exists. action='run' starts a "
            "graph; pass wait=true to run it inline and get the finished state "
            "back, otherwise poll with action='status' and the returned run_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "description": "list | read | run | status | cancel",
                },
                "workflow": {"type": "string", "description": "Workflow id or name (read, run)."},
                "run_id": {"type": "string", "description": "Run id (status, cancel)."},
                "payload": {"description": "Arbitrary JSON handed to the graph's steps (run)."},
                "wait": {
                    "type": "boolean",
                    "description": "run: execute inline and return the finished state instead of a run id.",
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
