"""The tool surface has to be able to finish what it starts.

These are about reachability, not plumbing. The engine could always author a
graph, answer a human step, advance a timer and read an event trail -- but
`tool.py` exposed none of it, so an unattended caller could drive a workflow
into `waiting_human` or `waiting_world` and had no move left except `cancel`.
A workflow engine a bot can start but not finish is not usable unattended.

Each test drives the tool exactly as a model does: a verb and a JSON string
back. Nothing reaches into the engine except to assert the result.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
import sys
import subprocess

import pytest

from tool import wfgraph_tool

_PLUGIN_DIR = str(Path(__file__).resolve().parents[2] / "plugins" / "wfgraph")


def call(**kwargs) -> dict:
    """Invoke the tool and parse its reply, as the model runtime would."""
    return json.loads(wfgraph_tool(**kwargs))


APPROVAL_GRAPH = {
    "steps": [
        {"id": "t", "kind": "trigger", "config": {}},
        {"id": "ask", "kind": "human", "config": {"title": "Approve", "prompt": "ship?"}},
        {"id": "done", "kind": "agent", "config": {"title": "Done", "goal": "finish"}},
    ],
    "edges": [
        {"id": "e1", "source": "t", "target": "ask"},
        {"id": "e2", "source": "ask", "target": "done"},
    ],
}


def test_a_bot_can_author_a_workflow_without_the_canvas(wf_home):
    """Authoring was GUI-only, so a bot could only run graphs a human drew."""
    saved = call(action="save", workflow="authored", scenario=APPROVAL_GRAPH)
    assert saved.get("saved") == "authored", saved

    listed = call(action="list")
    assert any(w["id"] == "authored" for w in listed["workflows"]), listed

    read = call(action="read", workflow="authored")
    assert [s["id"] for s in read["scenario"]["steps"]] == ["t", "ask", "done"]


def test_saving_an_unrunnable_graph_is_refused_not_stored(wf_home):
    """A broken graph in the store reads as real in `list` and fails later."""
    broken = {
        "steps": [
            {"id": "t", "kind": "trigger", "config": {}},
            {"id": "a", "kind": "agent", "config": {"goal": "x"}},
        ],
        # names a step that does not exist
        "edges": [{"id": "e1", "source": "t", "target": "ghost"}],
    }
    out = call(action="save", workflow="bad", scenario=broken)
    assert "error" in out, out
    assert "would not run" in out["error"]

    listed = call(action="list")
    assert not any(w["id"] == "bad" for w in listed["workflows"]), listed


def test_a_parked_run_says_what_would_unblock_it(wf_home):
    """`status` used to report waiting_human with no hint of the next move."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    started = call(action="run", workflow="appr", wait=True)

    state = call(action="status", run_id=started["runId"])
    assert state["status"] == "waiting_human"
    assert state["park"]["nodeId"] == "ask"
    assert state["unblock_with"] == "respond"


def test_a_bot_can_approve_a_human_step_and_the_run_continues(wf_home):
    """The blocker: no verb reached `respond`, so approval was impossible."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    started = call(action="run", workflow="appr", wait=True)
    run_id = started["runId"]

    answered = call(action="respond", run_id=run_id, answer="approved", note="ok by test")
    assert answered["answered"] == "ask", answered
    assert answered["status"] != "waiting_human", answered
    assert "ask" in answered["ran"], answered


def test_a_denial_halts_the_run_rather_than_continuing(wf_home):
    """Denial must not read as approval; it stops the graph."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    run_id = call(action="run", workflow="appr", wait=True)["runId"]

    out = call(action="respond", run_id=run_id, answer="denied", note="no")
    assert out["status"] == "failed", out
    assert "done" not in (out["ran"] or []), out


def test_responding_to_a_run_that_is_not_waiting_is_refused(wf_home):
    """An answer to a finished run must not be silently accepted."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    run_id = call(action="run", workflow="appr", wait=True)["runId"]
    call(action="respond", run_id=run_id, answer="approved")

    again = call(action="respond", run_id=run_id, answer="approved")
    assert "error" in again, again
    assert "not waiting" in again["error"]


def test_respond_without_an_answer_is_refused(wf_home):
    """An empty answer must not be read as either decision."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    run_id = call(action="run", workflow="appr", wait=True)["runId"]

    out = call(action="respond", run_id=run_id)
    assert "error" in out and "needs an answer" in out["error"], out


def test_a_bot_can_read_why_a_run_did_what_it_did(wf_home):
    """`status` gives the final shape; diagnosis needs the trail."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    run_id = call(action="run", workflow="appr", wait=True)["runId"]

    log = call(action="events", run_id=run_id)
    assert log["total"] > 0, log
    assert any(e.get("type") == "NodeStarted" for e in log["events"]), log


def test_the_event_trail_is_capped_so_one_call_cannot_flood_a_caller(wf_home):
    """A long rework loop's log is unbounded; an unbounded dump is a context bomb."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    run_id = call(action="run", workflow="appr", wait=True)["runId"]

    log = call(action="events", run_id=run_id, limit=1)
    assert len(log["events"]) == 1, log
    assert log["total"] >= 1


def test_a_bot_can_find_a_run_it_lost_the_id_for(wf_home):
    """Without `runs` a caller that dropped a run id could not recover it."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    mine = call(action="run", workflow="appr", wait=True)["runId"]

    listing = call(action="runs")
    assert any(r["runId"] == mine for r in listing["runs"]), listing

    scoped = call(action="runs", workflow="appr")
    assert all(r["workflow"] == "appr" for r in scoped["runs"]), scoped


def test_tick_advances_a_due_timer_wait(wf_home):
    """A park left behind by a dead process is resumable from a live one.

    `waits.py` arms an in-process timer thread when the wait starts, so in
    a single long-lived process the park resolves itself and `tick` finds
    nothing left to do. The case that needs this verb is the unattended one:
    the process that armed the timer is gone, and the park outlives it. This
    runs the wait in a subprocess, lets it die, and ticks from here.
    """
    graph = {
        "steps": [
            {"id": "t", "kind": "trigger", "config": {}},
            {
                "id": "hold",
                "kind": "wait",
                "config": {"until": {"type": "timer", "spec": "1s"}},
            },
        ],
        "edges": [{"id": "e1", "source": "t", "target": "hold"}],
    }
    call(action="save", workflow="timed", scenario=graph)

    script = (
        "import os,sys,json;"
        f"os.environ['HERMES_HOME']={str(wf_home)!r};"
        f"sys.path.insert(0,{_PLUGIN_DIR!r});"
        "import tool as T;"
        "print(json.loads(T.wfgraph_tool(action='run',"
        "workflow='timed', wait=True))['runId'])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    run_id = proc.stdout.strip().splitlines()[-1]

    parked = call(action="status", run_id=run_id)
    assert parked["status"] == "waiting_world", parked
    assert parked["unblock_with"] == "tick", parked

    time.sleep(1.2)
    ticked = call(action="tick", run_id=run_id)
    assert run_id in ticked["resumed"], ticked


def test_deleting_a_workflow_removes_it_from_the_list(wf_home):
    call(action="save", workflow="temp", scenario=APPROVAL_GRAPH)
    assert call(action="delete", workflow="temp")["deleted"] == "temp"
    assert not any(w["id"] == "temp" for w in call(action="list")["workflows"])


def test_deleting_something_that_is_not_there_is_an_error_not_a_shrug(wf_home):
    out = call(action="delete", workflow="never-existed")
    assert "error" in out, out


def test_an_unknown_verb_lists_the_real_ones(wf_home):
    """The model's recovery path when it guesses a verb."""
    out = call(action="frobnicate")
    assert "error" in out
    for verb in ("save", "respond", "tick", "events", "runs"):
        assert verb in out["error"]
