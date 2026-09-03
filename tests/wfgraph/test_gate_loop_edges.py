"""A gate loops only along an edge the author marked as a loop.

`_run_gate` decided "this is a rework loop" by asking whether the route target
had already run:

    if route in state["ran"]:

That is not what a loop is. The scenario declares loops explicitly --
``{"source": "check", "target": "work", "loop": True}`` -- and the rest of the
engine already honours the flag (`topology.is_loop`, `preds(..., loops=False)`).

The gap bites on any graph where a forward target can already have run. A gate
that fans in from two arms evaluates twice; on the second pass its `done` arm
routes to a `report` step that ran on the first pass, the engine reads
"already in ran" as "loop", and ships the workflow a second time -- burning a
maxLoops budget meant for rework and re-running whatever the final step does.

The declared flag is the authority. `ran` is a coincidence.
"""

from __future__ import annotations

import pytest

from wfgraph.runner import start_run
from wfgraph.store import load_events, load_run, save_documents

pytestmark = pytest.mark.usefixtures("wf_home")


def _fan_in_graph() -> dict:
    """work fans out to audit and check; both fan back into check.

    The extra arm is what makes the gate evaluate twice, which is what exposes
    a forward edge to the already-ran test.
    """
    return {
        "id": "loop",
        "name": "Loop",
        "scenario": {
            "steps": [
                {"id": "t", "kind": "trigger", "title": "t"},
                {"id": "work", "kind": "agent", "title": "work"},
                {"id": "audit", "kind": "agent", "title": "audit"},
                {
                    "id": "check",
                    "kind": "gate",
                    "title": "check",
                    "maxLoops": 5,
                    "arms": [
                        {"id": "redo", "when": {"mode": "any-fail"}},
                        {"id": "done", "when": {"mode": "always"}},
                    ],
                },
                {"id": "report", "kind": "agent", "title": "report"},
            ],
            "edges": [
                {"source": "t", "target": "work"},
                {"source": "work", "target": "audit"},
                {"source": "work", "target": "check"},
                {"source": "audit", "target": "check"},
                # the only loop the author drew
                {"source": "check", "target": "work", "sourceHandle": "redo", "loop": True},
                # a plain forward edge
                {"source": "check", "target": "report", "sourceHandle": "done"},
            ],
        },
    }


def _run(doc: dict, fails: int = 1):
    save_documents([doc], current_id=doc["id"])
    seen: list[str] = []

    def execute(goal, context, payload, cfg):
        node = str(goal).strip()
        seen.append(node)
        if node == "work":
            verdict = "FAIL" if seen.count("work") <= fails else "PASS"
            return {"ok": True, "text": "attempt", "verdict": verdict}
        return {"ok": True, "text": "ok", "verdict": "PASS"}

    state = start_run(doc["id"], payload={}, background=False, execute_fn=execute)
    return state, seen


def test_a_forward_arm_does_not_re_run_a_step_that_already_ran():
    """The `done` arm ships once, even though `report` is already in `ran`."""
    state, seen = _run(_fan_in_graph())

    assert seen.count("report") == 1, f"shipped more than once: {seen}"
    assert state["status"] == "succeeded"


def test_a_forward_arm_does_not_burn_the_rework_budget():
    """`loops` counts rework takes. One FAIL is one loop, not two."""
    state, seen = _run(_fan_in_graph())

    assert state["loops"] == 1, f"loops={state['loops']} for a single FAIL: {seen}"


def test_only_a_declared_loop_edge_emits_loopadvanced():
    """LoopAdvanced must name a target the author wired with loop=True."""
    state, _ = _run(_fan_in_graph())

    declared = {
        e["target"]
        for e in _fan_in_graph()["scenario"]["edges"]
        if e.get("loop")
    }
    advanced = [
        ev["payload"]["to"]
        for ev in load_events(state["runId"])
        if ev["type"] == "LoopAdvanced"
    ]

    assert advanced, "the FAIL should still produce a real loop"
    assert set(advanced) <= declared, f"looped along an undeclared edge: {advanced}"


def test_the_real_rework_loop_still_works():
    """The fix must not cost the behaviour the loop exists for."""
    state, seen = _run(_fan_in_graph(), fails=1)

    assert seen.count("work") == 2, f"rework did not re-run work: {seen}"
    assert state["status"] == "succeeded"


def test_a_gate_that_never_fails_never_loops():
    state, seen = _run(_fan_in_graph(), fails=0)

    assert seen.count("work") == 1, seen
    assert state["loops"] == 0
    assert seen.count("report") == 1, seen
