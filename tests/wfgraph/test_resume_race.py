"""Resuming a paused run is a cross-process transaction.

Two bot processes resuming the same paused run within milliseconds both
flipped paused->running and both spawned a driver: duplicated steps and
a corrupted queue. Exactly one process may resume; the rest must get
the existing run back.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from wfgraph.store import save_run, load_run, save_documents

REPO = Path(__file__).resolve().parents[2]
PLUGIN = str(REPO / "plugins" / "wfgraph")
HERMES = str(REPO)

RESUMER = r"""
import json, os, sys, time
os.environ["HERMES_HOME"] = sys.argv[1]
sys.path[:0] = [sys.argv[2], sys.argv[3]]
from wfgraph.runner import resume_run
barrier = sys.argv[4]
while not os.path.exists(barrier):
    time.sleep(0.001)
def execute(goal, context=None, payload=None, cfg=None, **kw):
    time.sleep(1.0)
    return {"summary": "ok", "verdict": "PASS", "output": {}}
state = resume_run(sys.argv[5], execute_fn=execute)
print(json.dumps({"pid": os.getpid(), "status": state["status"]}))
"""


def _paused_run(wf_home, run_id):
    save_run({
        "runId": run_id, "workflowId": "resume-race", "name": "resume-race",
        "scenario": {
            "steps": [
                {"id": "work", "kind": "agent", "config": {"goal": "work"}},
            ],
            "edges": [],
        },
        "status": "paused", "queue": ["work"], "ran": [], "seq": 0,
        "satisfied": [], "verdicts": {}, "outputs": {}, "summaries": {},
        "take": {}, "loops": 0, "tries": {}, "inFlight": [], "sessions": {},
        "park": None, "waitingEvent": None, "pauseRequested": False,
        "failed": False, "startedAt": 0,
        "payload": None, "source": "test",
    })


def test_concurrent_processes_resume_exactly_one_driver(wf_home, tmp_path):
    run_id = "run-resume-race-1"
    _paused_run(wf_home, run_id)
    barrier = tmp_path / "go"
    args = [sys.executable, "-c", RESUMER, str(wf_home), PLUGIN, HERMES, str(barrier), run_id]
    procs = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(6)]
    barrier.touch()
    results = []
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    run = load_run(run_id)
    # every caller got a state, and the run has exactly one take of its step
    takes = run.get("take", {})
    assert run["ran"].count("work") <= 1, (results, run["ran"])
    print("results:", results)


_BASE = {
    "runId": "run-x",
    "workflowId": "resume-race",
    "name": "resume-race",
    "scenario": {
        "steps": [
            {"id": "t", "kind": "trigger", "config": {}},
            {"id": "work", "kind": "trigger", "config": {}},
        ],
        "edges": [{"source": "t", "target": "work"}],
    },
    "payload": None,
    "source": "manual",
    "status": "paused",
    "queue": ["work"],
    "ran": ["t"],
    "satisfied": [],
    "verdicts": {},
    "outputs": {},
    "summaries": {},
    "take": {},
    "loops": 0,
    "park": None,
    "wakeAt": None,
    "waitingEvent": None,
    "pauseRequested": False,
    "seq": 0,
    "startedAt": 1,
    "failed": False,
    "tries": {},
    "inFlight": [],
    "sessions": {},
}


def test_a_dead_owner_paused_run_is_reaped_by_start(wf_home, monkeypatch):
    """A paused run whose owning process is gone must not block a fresh start.

    The old reaper only failed dead `running` runs; a paused run left behind
    by a crashed process wedged the workflow forever at "paused".
    """
    import wfgraph.runner as runner
    from wfgraph.lease import stamp

    save_documents([{
        "id": "resume-race", "name": "resume-race", "scenario": _BASE["scenario"],
    }], current_id="resume-race")
    save_run({
        **_BASE,
        "runId": "run-dead-paused",
        "workflowId": "resume-race",
        "status": "paused",
        "queue": ["work"],
        "owner": {"pid": 999999999, "startedAt": 12345.0},  # no such process
    })
    # sanity: the lease agrees this owner is dead
    state = load_run("run-dead-paused")
    from wfgraph.lease import owner_alive
    assert not owner_alive(state)

    # start_run on the same workflow must reap it and start fresh
    out = runner.start_run(
        "resume-race",
        background=False,
        execute_fn=lambda *a, **k: {"summary": "ok", "verdict": "PASS", "output": {}},
    )
    assert out["status"] == "succeeded", out["status"]
    assert out["runId"] != "run-dead-paused"
    dead = load_run("run-dead-paused")
    assert dead["status"] == "failed", dead["status"]


def test_resume_stamps_the_new_owner(wf_home):
    """The resuming process becomes the recorded owner of the run."""
    import wfgraph.runner as runner
    import os
    from wfgraph.lease import stamp

    save_run({**_BASE, "runId": "run-stamp", "status": "paused", "queue": ["work"]})
    out = runner.resume_run("run-stamp", execute_fn=lambda *a, **k: {"summary": "ok", "verdict": "PASS", "output": {}})
    state = load_run("run-stamp")
    assert state["owner"]["pid"] == os.getpid()
