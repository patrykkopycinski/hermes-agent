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


def test_saving_from_to_edges_is_refused_not_stored(wf_home):
    """from/to edges silently turned every node into a root (2026-09-03)."""
    broken = {
        "steps": [
            {"id": "t", "kind": "trigger", "config": {}},
            {"id": "a", "kind": "agent", "config": {"goal": "x"}},
        ],
        "edges": [{"from": "t", "to": "a"}],
    }
    out = call(action="save", workflow="fromto", scenario=broken)
    assert "error" in out, out
    assert "source/target" in out["error"], out


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


def test_a_parked_status_says_what_it_is_waiting_on(wf_home):
    """Knowing a run is blocked is not enough to unblock it.

    `respond` needs a node id, and a human deciding needs the question.
    Both live in the run's park; a status that reports only "waiting_human"
    leaves a caller with the verb and no arguments for it.
    """
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    run_id = call(action="run", workflow="appr", wait=True)["runId"]

    st = call(action="status", run_id=run_id)
    waiting = st.get("waiting_on")
    assert waiting, st
    assert waiting["node_id"] == "ask", waiting
    assert "approve" in (waiting.get("prompt") or "").lower(), waiting
    assert waiting.get("kind") == "human", waiting


def test_the_parked_status_carries_enough_to_call_respond(wf_home):
    """The end-to-end point: status -> respond with no guessing."""
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    run_id = call(action="run", workflow="appr", wait=True)["runId"]

    st = call(action="status", run_id=run_id)
    out = call(
        action=st["unblock_with"],
        run_id=run_id,
        node_id=st["waiting_on"]["node_id"],
        answer="approved",
    )
    assert out["status"] != "waiting_human", out


def test_answering_the_wrong_step_is_refused(wf_home):
    """A node_id that disagrees with the park is a stale read, not a hint.

    Honouring it would resolve a question nobody asked -- the run moved
    on between the status call and the answer.
    """
    call(action="save", workflow="appr", scenario=APPROVAL_GRAPH)
    run_id = call(action="run", workflow="appr", wait=True)["runId"]

    out = call(
        action="respond", run_id=run_id, node_id="some-other-step",
        answer="approved",
    )
    assert "error" in out, out
    assert "ask" in out["error"], out
    assert call(action="status", run_id=run_id)["status"] == "waiting_human"


def test_run_finishes_before_it_returns_by_default(wf_home):
    """A tool call that outlives its process is not a run, it is a leak.

    The engine defaults `start_run(background=True)`, which is right for a
    desktop app holding a window open and wrong for every caller here: a
    cron job, a subagent, a shell. Those processes exit as soon as the tool
    returns, taking the worker thread with them, and the run is stranded
    mid-flight with no error anywhere.

    Proven across a real process boundary: a subprocess that starts a run
    and exits immediately must leave a finished run behind, not a running
    one. In-process this would pass either way -- the thread would just
    keep going -- so the boundary is the whole test.
    """
    graph = {
        "steps": [
            {"id": "t", "kind": "trigger", "config": {}},
            {"id": "hold", "kind": "wait",
             "config": {"until": {"type": "timer", "spec": "1s"}}},
        ],
        "edges": [{"id": "e1", "source": "t", "target": "hold"}],
    }
    call(action="save", workflow="sync", scenario=graph)

    script = (
        "import os,sys,json;"
        f"os.environ['HERMES_HOME']={str(wf_home)!r};"
        f"sys.path.insert(0,{_PLUGIN_DIR!r});"
        "import tool as T;"
        "r=json.loads(T.wfgraph_tool(action='run', workflow='sync'));"
        "print(r['runId'], r['status'])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    run_id, reported = proc.stdout.strip().split()

    # The run reached its park before the process died -- not "running",
    # which would mean the caller got a handle to a thread that no longer
    # exists.
    assert reported == "waiting_world", proc.stdout
    assert call(action="status", run_id=run_id)["status"] == "waiting_world"


def test_the_tool_tolerates_dispatcher_injected_kwargs(wf_home):
    """The dispatcher injects task_id/session context the tool never declared.

    A real agent turn under a bot profile failed with
    ``TypeError: wfgraph_tool() got an unexpected keyword argument 'task_id'``
    before any verb ran. Plugin tools must tolerate injected call context.
    """
    out = wfgraph_tool(action="list", task_id="abc", session_id="s", anything=1)
    assert '"error"' not in out


def test_the_tool_accepts_a_single_packed_dict_of_arguments(wf_home):
    """The dispatcher invokes plugin tools with one positional params dict.

    A real agent turn failed with ``AttributeError: 'dict' object has no
    attribute 'strip'`` on every action, including ones needing no args.
    """
    out = wfgraph_tool({"action": "list"})
    assert '"error"' not in out
    out = wfgraph_tool(
        {
            "action": "save",
            "workflow": "packed",
            "scenario": {"steps": [{"id": "t", "kind": "trigger", "config": {}}], "edges": []},
        }
    )
    assert '"saved": "packed"' in out


def test_sync_verb_creates_a_cron_job_for_a_cron_workflow(wf_home, monkeypatch):
    """action='sync' must expose trigger sync to bots without python imports."""
    import sys, types
    from tests.wfgraph.test_cron_sync import FakeCron  # reuse the cron stub

    fake = FakeCron()
    module = types.ModuleType("cron.jobs")
    module.create_job = fake.create_job
    module.update_job = fake.update_job
    module.remove_job = fake.remove_job
    module.list_jobs = fake.list_jobs
    package = types.ModuleType("cron")
    package.jobs = module
    monkeypatch.setitem(sys.modules, "cron", package)
    monkeypatch.setitem(sys.modules, "cron.jobs", module)

    wfgraph_tool(
        action="save",
        workflow="syncwf",
        scenario={
            "steps": [
                {"id": "t", "kind": "trigger", "config": {"on": {"type": "cron", "spec": "*/5 * * * *"}}}
            ],
            "edges": [],
        },
    )
    out = json.loads(wfgraph_tool(action="sync"))
    assert "error" not in out, out
    assert len(out.get("cron", [])) == 1, out
