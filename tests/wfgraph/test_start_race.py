"""Starting one workflow is a transaction across processes.

A module-level lock only serializes threads in one interpreter. Cron, webhook,
and bot tool calls are separate processes, so two of them can both observe
"no active run" and mint duplicates.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from wfgraph.store import upsert_document

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "wfgraph"
HERMES_AGENT = REPO

STARTER = r'''
import json, os, sys, time
os.environ["HERMES_HOME"] = sys.argv[1]
sys.path[:0] = [sys.argv[2], sys.argv[3]]
from wfgraph.runner import start_run
barrier = sys.argv[4]
while not os.path.exists(barrier):
    time.sleep(0.001)
def execute(*args, **kwargs):
    # Keep the first run active long enough for every concurrent process to
    # inspect it. The transaction prevents duplicate *active* starts; a run
    # deliberately started after completion is a legitimate new invocation.
    time.sleep(1.0)
    return {"summary": "ok", "verdict": "PASS", "output": {}}
state = start_run("race", background=False, execute_fn=execute)
print(json.dumps({"runId": state["runId"], "status": state["status"]}))
'''


def test_concurrent_processes_start_exactly_one_run(wf_home, tmp_path):
    upsert_document(
        {
            "id": "race",
            "name": "race",
            "scenario": {
                "steps": [
                    {"id": "work", "kind": "agent", "config": {"goal": "hold"}}
                ],
                "edges": [],
            },
        }
    )
    barrier = tmp_path / "go"
    env = os.environ.copy()
    args = [
        sys.executable,
        "-c",
        STARTER,
        str(wf_home),
        str(PLUGIN),
        str(HERMES_AGENT),
        str(barrier),
    ]
    processes = [
        subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(12)
    ]
    barrier.touch()

    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout.strip().splitlines()[-1]))

    run_ids = {result["runId"] for result in results}
    run_files = list((wf_home / "workflows" / "runs").glob("*.json"))
    assert len(run_ids) == 1, results
    assert len(run_files) == 1, [path.name for path in run_files]
