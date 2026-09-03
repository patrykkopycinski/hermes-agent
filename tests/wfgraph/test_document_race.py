"""Document authoring is transactional across bot processes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "wfgraph"

WRITER = r'''
import os, sys, time
os.environ["HERMES_HOME"] = sys.argv[1]
sys.path[:0] = [sys.argv[2], sys.argv[3]]
from wfgraph.store import upsert_document
barrier = sys.argv[4]
while not os.path.exists(barrier):
    time.sleep(0.001)
i = sys.argv[5]
upsert_document({"id": "w" + i, "name": "w" + i, "scenario": {"steps": [], "edges": []}})
'''


def test_parallel_document_creates_do_not_overwrite_each_other(wf_home, tmp_path):
    barrier = tmp_path / "go"
    args = [sys.executable, "-c", WRITER, str(wf_home), str(PLUGIN), str(REPO), str(barrier)]
    processes = [
        subprocess.Popen([*args, str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(30)
    ]
    barrier.touch()
    for process in processes:
        _stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr

    raw = json.loads((wf_home / "workflows" / "documents.json").read_text())
    ids = {doc["id"] for doc in raw["docs"]}
    assert ids == {f"w{i}" for i in range(30)}
