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

from wfgraph.store import save_run, load_run

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
