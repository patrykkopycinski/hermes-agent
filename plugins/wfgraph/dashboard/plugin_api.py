"""Read-only HTTP view of wfgraph runs, for the desktop viewer pane.

Runs inside the gateway process, so it imports the engine's own store rather
than re-parsing JSON off disk -- that means the pane sees exactly what the
runner sees, including the orphan reaping in ``load_run`` that turns a run
whose process died into an honest ``failed`` instead of a phantom
``running``.

Read-only on purpose. Cancelling from here would need the on-disk signal
path and a confirmation gesture; a viewer that can silently kill a
production run is not a viewer.
"""

from __future__ import annotations

import os
import sys

from fastapi import APIRouter

router = APIRouter()

# The engine lives in the sibling Python plugin. The gateway may not have it on
# sys.path, so add the plugin root (this file is <plugin>/dashboard/plugin_api.py).
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

_TERMINAL = {"succeeded", "failed", "cancelled"}


def _steps_of(scenario: dict) -> list[dict]:
    raw = (scenario or {}).get("steps")
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _shape(run: dict, scenario: dict | None = None, name: str | None = None) -> dict:
    """Flatten a run record into what the pane draws."""
    ran = run.get("ran") or []
    verdicts = run.get("verdicts") or {}
    status = run.get("status")

    steps = []
    for step in _steps_of(scenario or {}):
        sid = step.get("id")
        if not sid:
            continue
        in_flight = sid in (run.get("inFlight") or [])
        if sid in ran:
            state = "ran"
        elif status in _TERMINAL:
            # A terminal run has nothing in flight. The engine can leave a step
            # in inFlight when the run dies mid-step (crash, orphan reap), and
            # trusting that flag paints a finished run with a step spinning
            # "running" forever -- the viewer twin of the orphaned-run defect.
            state = "stopped" if in_flight else "skipped"
        elif in_flight:
            state = "running"
        else:
            state = "pending"
        steps.append(
            {
                "id": sid,
                "kind": step.get("kind"),
                "title": step.get("title") or sid,
                "state": state,
                "verdict": verdicts.get(sid),
                "summary": (run.get("summaries") or {}).get(sid),
            }
        )

    park = run.get("park") or {}
    return {
        "runId": run.get("runId"),
        "workflow": run.get("workflowId") or run.get("workflow"),
        # The pane titles each row with this. Falling back to the workflow id
        # beats a blank row when the document was renamed or deleted.
        "name": name or run.get("name") or run.get("workflowId") or "(unnamed)",
        "status": status,
        "error": run.get("error"),
        "ran": ran,
        "loops": run.get("loops") or 0,
        "startedAt": run.get("startedAt"),
        "wakeAt": run.get("wakeAt"),
        "parkedOn": park.get("nodeId"),
        "parkedUntil": park.get("until"),
        "steps": steps,
    }


def _docs_by_id() -> dict:
    """``load_documents`` returns {"docs": [...], "currentId": ...} -- not a list."""
    from wfgraph.store import load_documents

    payload = load_documents() or {}
    docs = payload.get("docs") if isinstance(payload, dict) else None
    return {d["id"]: d for d in (docs or []) if isinstance(d, dict) and d.get("id")}


@router.get("/runs")
async def runs(limit: int = 25):
    """Recent runs, newest first. Reaped through the engine's own reader."""
    from wfgraph.store import list_runs

    docs = _docs_by_id()
    out = []
    for run in list_runs():
        if not isinstance(run, dict):
            continue
        wf = run.get("workflowId") or run.get("workflow")
        scenario = (docs.get(wf) or {}).get("scenario") or {}
        out.append(_shape(run, scenario, (docs.get(wf) or {}).get("name")))
    out.sort(key=lambda r: r.get("startedAt") or 0, reverse=True)
    return {"runs": out[: max(1, min(limit, 200))]}


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, after: int = -1):
    """One run plus its event tail."""
    from wfgraph.store import load_events, load_run

    run = load_run(run_id)
    if not isinstance(run, dict):
        return {"error": f"No run '{run_id}'."}

    wf = run.get("workflowId") or run.get("workflow")
    doc = _docs_by_id().get(wf) or {}
    scenario = doc.get("scenario") or {}

    events = load_events(run_id, after) or []
    detail = _shape(run, scenario, doc.get("name"))
    # Event tail only. Edges were shipped here too, but nothing rendered them --
    # the pane draws the step list, not a node graph. Dead payload, dropped.
    detail["events"] = events[-100:]
    return detail


@router.get("/workflows")
async def workflows():
    out = []
    for doc in _docs_by_id().values():
        out.append(
            {
                "id": doc["id"],
                "name": doc.get("name") or doc["id"],
                "steps": len(_steps_of(doc.get("scenario") or {})),
            }
        )
    return {"workflows": out}
