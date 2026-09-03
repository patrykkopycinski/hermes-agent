"""The persisted event counter must be unique across processes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from wfgraph.store import save_run

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "wfgraph"

WRITER = r'''
import os, sys, time
os.environ["HERMES_HOME"] = sys.argv[1]
sys.path[:0] = [sys.argv[2], sys.argv[3]]
from wfgraph.store import append_event
barrier = sys.argv[4]
while not os.path.exists(barrier):
    time.sleep(0.001)
append_event("r", "E")
'''


def test_parallel_event_appends_allocate_unique_sequences(wf_home, tmp_path):
    save_run({"runId": "r", "workflowId": "w", "status": "waiting_world", "seq": 0})
    barrier = tmp_path / "go"
    args = [sys.executable, "-c", WRITER, str(wf_home), str(PLUGIN), str(REPO), str(barrier)]
    processes = [
        subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(40)
    ]
    barrier.touch()
    for process in processes:
        _stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr

    events = [
        json.loads(line)
        for line in (wf_home / "workflows" / "runs" / "r.jsonl").read_text().splitlines()
    ]
    seqs = [event["seq"] for event in events]
    state = json.loads((wf_home / "workflows" / "runs" / "r.json").read_text())
    assert sorted(seqs) == list(range(40)), seqs
    assert state["seq"] == 40
