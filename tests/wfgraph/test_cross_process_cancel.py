"""Cancel must cross a process boundary.

An operator cancels from wherever they are -- the CLI, the tool surface, a
second gateway worker -- while the run executes somewhere else. `signal()`
kept its pending cancels in a module-level dict, so the request died with
the process that made it and the run finished as if nothing happened. The
caller still got back `{"signalled": "cancel"}`, so it looked handled.

Same cross-process class as the ownership bug: in-memory state cannot
coordinate two processes.
"""

import json
import os
import subprocess
import sys

import pytest

from wfgraph.runner import start_run
from wfgraph.store import save_documents

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = os.path.join(REPO, "plugins", "wfgraph")

WF = {
    "id": "cancelwf",
    "name": "cancelwf",
    "scenario": {
        "steps": [
            {"id": "t", "kind": "trigger", "title": "t"},
            {"id": "slow", "kind": "agent", "title": "slow"},
            {"id": "after", "kind": "agent", "title": "after"},
        ],
        "edges": [
            {"source": "t", "target": "slow"},
            {"source": "slow", "target": "after"},
        ],
    },
}

CANCEL_SCRIPT = """
import os, sys
os.environ["HERMES_HOME"] = sys.argv[1]
sys.path.insert(0, sys.argv[2])
from wfgraph.runtime import signal
signal(sys.argv[3], "cancel")
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return str(tmp_path)


def _cancel_from_another_process(home_dir: str, run_id: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", CANCEL_SCRIPT, home_dir, PLUGIN, run_id],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_cancel_from_another_process_stops_the_run(home):
    """A cancel raised in a different process must actually stop this run."""
    save_documents([WF], current_id="cancelwf")

    seen = {}
    import wfgraph.runner as runner_mod

    original = runner_mod._fresh_state

    def remember(*args, **kwargs):
        state = original(*args, **kwargs)
        seen["rid"] = state["runId"]
        return state

    runner_mod._fresh_state = remember
    try:
        def execute(goal, context, payload, cfg=None, **kw):
            if str(goal).strip() == "slow":
                _cancel_from_another_process(home, seen["rid"])
            return {"ok": True, "text": "done", "verdict": "PASS"}

        state = start_run("cancelwf", payload={}, background=False, execute_fn=execute)
    finally:
        runner_mod._fresh_state = original

    assert state["status"] == "cancelled", (
        f"cancel from another process was ignored; run ended {state['status']!r}"
    )
    assert "after" not in state["ran"], (
        f"run kept going after cancel; ran {state['ran']}"
    )
