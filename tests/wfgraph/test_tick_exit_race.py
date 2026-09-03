"""A tick from a short-lived process must not strand the work it resumes.

`_resume` ends with `spawn()`, a daemon thread in whatever process ticked.
A cron job or bot tool call ticks and exits; the daemon dies with it; the
reaper then marks the run failed. The resumed work must run in the
foreground of the ticking process so it finishes before that process exits.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from wfgraph.store import save_documents, load_run

REPO = Path(__file__).resolve().parents[2]
PLUGIN = str(REPO / "plugins" / "wfgraph")
HERMES = str(REPO)

# A ticking process that exits immediately after ticking -- exactly what a
# cron job or bot tool call does.
TICKER = r"""
import os, sys, time
os.environ["HERMES_HOME"] = sys.argv[1]
sys.path[:0] = [sys.argv[2], sys.argv[3]]
from wfgraph.runner import start_run

def execute(goal, context=None, payload=None, cfg=None, **kw):
    time.sleep(0.3)  # the resumed step takes a moment; the ticker must not leave early
    return {"summary": "ok", "text": "done", "verdict": "PASS"}

mode = sys.argv[4]
if mode == "park":
    # Park a 1s timer inline and EXIT at once: the arming thread dies unfired,
    # exactly like a cron trigger process that parks and returns.
    state = start_run("tickwf", background=False, execute_fn=execute)
    print("PARKED", state.get("status"))
else:
    from wfgraph.waits import tick_timers
    print("RESUMED", len(tick_timers()))
"""


def _doc():
    return {
        "id": "tickwf", "name": "tickwf",
        "scenario": {
            "steps": [
                {"id": "t", "kind": "trigger", "title": "t"},
                {"id": "hold", "kind": "wait", "title": "hold",
                 "config": {"until": {"type": "timer", "spec": "1s"}}},
                {"id": "after", "kind": "trigger", "title": "after",
                 "config": {}},
            ],
            "edges": [
                {"source": "t", "target": "hold"},
                {"source": "hold", "target": "after"},
            ],
        },
    }


def test_a_tick_that_exits_still_finishes_the_run(wf_home):
    save_documents([_doc()], current_id="tickwf")
    park = subprocess.run(
        [sys.executable, "-c", TICKER, str(wf_home), PLUGIN, HERMES, "park"],
        capture_output=True, text=True, timeout=60,
    )
    assert park.returncode == 0, park.stderr
    assert "PARKED waiting_world" in park.stdout, park.stdout

    time.sleep(1.3)  # past due; nothing is alive to resume it

    tick = subprocess.run(
        [sys.executable, "-c", TICKER, str(wf_home), PLUGIN, HERMES, "tick"],
        capture_output=True, text=True, timeout=60,
    )
    assert tick.returncode == 0, tick.stderr
    assert "RESUMED 1" in tick.stdout, tick.stdout

    time.sleep(0.5)
    run = load_run("run-of-tickwf") if False else None
    from wfgraph.store import list_runs
    runs = list_runs("tickwf")
    assert runs, "no run persisted"
    final = runs[-1]
    assert final["status"] == "succeeded", (
        f"run ended {final['status']} after a tick-and-exit; a cron or bot")
    assert "after" in final.get("ran", []), final.get("ran")
