"""A run whose process exited must not sit at "running" forever.

`hermes workflow run` (and every cron / webhook trigger) spawns the graph on
a daemon thread and returns. The process exits, the thread is killed
mid-step, and the run is left at status "running" with no steps done and no
error -- unreadable to an operator and, because it still looks live, not
safely restartable.

The lease already knows the truth (owner_alive is False). These tests pin
that the engine ACTS on it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from wfgraph.store import load_run, save_documents, save_run

pytestmark = pytest.mark.usefixtures("wf_home")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = os.path.join(REPO, "plugins", "wfgraph")

# A trigger process that spawns a slow run and exits immediately, exactly
# like a cron firing `hermes workflow run`.
TRIGGER = """
import os, sys, time
os.environ["HERMES_HOME"] = sys.argv[1]
sys.path.insert(0, {plugin!r})
from wfgraph.store import save_documents
from wfgraph.runner import start_run
save_documents([{{
    "id": "cronwf", "name": "cronwf",
    "scenario": {{
        "steps": [
            {{"id": "t", "kind": "trigger", "title": "t"}},
            {{"id": "slow", "kind": "agent", "title": "slow"}},
        ],
        "edges": [{{"source": "t", "target": "slow"}}],
    }},
}}], current_id="cronwf")

def execute(goal, context, payload, cfg=None, **kw):
    time.sleep(30)
    return {{"ok": True, "text": "done", "verdict": "PASS"}}

out = start_run("cronwf", payload={{}}, background=True, execute_fn=execute)
print(out["runId"])
"""


def _fire_and_exit(home: str) -> str:
    """Run a trigger in a separate process that exits mid-step."""
    script = TRIGGER.format(plugin=PLUGIN)
    proc = subprocess.run(
        [sys.executable, "-c", script, home],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"trigger failed: {proc.stderr}"
    return proc.stdout.strip().splitlines()[-1]


def test_run_orphaned_by_process_exit_is_not_reported_as_running(wf_home):
    """The stranded run must read as failed, not as a live run."""
    run_id = _fire_and_exit(str(wf_home))
    time.sleep(1)  # the spawning process is gone; nothing can advance this

    run = load_run(run_id)
    assert run is not None
    assert run["status"] != "running", (
        f"run {run_id} still reads 'running' after its process exited; "
        "an operator cannot tell it is dead and it blocks a re-run"
    )
    assert run["status"] == "failed"


def test_orphaned_run_says_why_it_died(wf_home):
    """A dead run needs a reason on the record, not a bare status."""
    run_id = _fire_and_exit(str(wf_home))
    time.sleep(1)

    run = load_run(run_id)
    blob = f"{run.get('error')} {run.get('errors')}"
    assert "died" in blob or "process" in blob, (
        f"orphaned run stored no reason: error={run.get('error')!r}"
    )
