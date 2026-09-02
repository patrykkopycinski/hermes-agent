"""A failed run must record WHY it failed on the run itself.

Found by driving the real agent path (no stub execute_fn): a missing model
returns {"ok": False, "error": "Model parameter is required..."} and the
engine correctly failed the run -- but persisted no reason anywhere on the
run record, so a cron or webhook operator reading the stored run sees
"failed" and nothing else.
"""

from __future__ import annotations

import pytest

from wfgraph.runner import start_run
from wfgraph.store import load_run, save_documents

pytestmark = pytest.mark.usefixtures("wf_home")


def _wf(wid: str = "errwf") -> dict:
    return {
        "id": wid,
        "name": wid,
        "scenario": {
            "steps": [
                {"id": "t", "kind": "trigger", "name": "start"},
                {"id": "a", "kind": "agent", "name": "step a", "config": {"goal": "do it"}},
            ],
            "edges": [{"source": "t", "target": "a"}],
        },
    }


def test_user_fixable_error_is_recorded_on_the_run():
    """A config error the user must fix has to survive on the run record."""
    save_documents([_wf()])

    def boom(goal, context, payload, config=None, **kw):
        return {"ok": False, "error": "Model parameter is required. Set a model on this step."}

    out = start_run("errwf", payload={}, background=False, execute_fn=boom)

    assert out["status"] == "failed"
    run = load_run(out["runId"])
    blob = f"{run.get('error')} {run.get('errors')} {run.get('summaries')}"
    assert "Model parameter is required" in blob, (
        f"run {out['runId']} failed but stored no reason: error={run.get('error')!r} "
        f"errors={run.get('errors')!r} summaries={run.get('summaries')!r}"
    )


def test_plain_step_failure_is_recorded_on_the_run():
    """Any failing step, fixable or not, must name itself on the run record."""
    save_documents([_wf("errwf2")])

    def boom(goal, context, payload, config=None, **kw):
        return {"ok": False, "error": "upstream 503 from the tool backend"}

    out = start_run("errwf2", payload={}, background=False, execute_fn=boom)

    assert out["status"] == "failed"
    run = load_run(out["runId"])
    blob = f"{run.get('error')} {run.get('errors')} {run.get('summaries')}"
    assert "upstream 503" in blob, (
        f"run {out['runId']} failed but stored no reason: error={run.get('error')!r} "
        f"errors={run.get('errors')!r}"
    )
    assert "a" in blob, "the failing step id should be identifiable on the run"


def test_loop_cap_exhaustion_is_recorded_on_the_run():
    """Giving up after N takes must say so on the run, not just in the stream."""
    save_documents([
        {
            "id": "loopwf",
            "name": "loopwf",
            "scenario": {
                "steps": [
                    {"id": "t", "kind": "trigger", "title": "t"},
                    {"id": "work", "kind": "agent", "title": "work"},
                    {
                        "id": "check",
                        "kind": "gate",
                        "title": "check",
                        "maxLoops": 2,
                        "arms": [
                            {"id": "redo", "when": {"mode": "any-fail"}},
                            {"id": "done", "when": {"mode": "always"}},
                        ],
                    },
                    {"id": "report", "kind": "agent", "title": "report"},
                ],
                "edges": [
                    {"source": "t", "target": "work"},
                    {"source": "work", "target": "check"},
                    {"source": "check", "target": "work", "sourceHandle": "redo", "loop": True},
                    {"source": "check", "target": "report", "sourceHandle": "done"},
                ],
            },
        }
    ])

    def always_fail(goal, context, payload, cfg=None, **kw):
        node = str(goal).strip()
        if node == "work":
            return {"ok": True, "text": "attempt", "verdict": "FAIL"}
        return {"ok": True, "text": "ok", "verdict": "PASS"}

    out = start_run("loopwf", payload={}, background=False, execute_fn=always_fail)
    assert out["status"] == "failed"
    run = load_run(out["runId"])
    blob = f"{run.get('error')} {run.get('errors')}"
    assert "gave up after" in blob, (
        f"loop-cap run stored no reason: error={run.get('error')!r} errors={run.get('errors')!r}"
    )
