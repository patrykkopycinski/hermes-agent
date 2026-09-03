"""Shipping through a gate twice must not spend the retry budget.

Found by random search over 400 gate graphs: 8 of them behave differently when
`_run_gate` decides "this is a loop" by asking `route in state["ran"]` instead
of reading the edge's declared `loop` flag. The visible damage is the loop
counter -- `loops: 2` where only one rework take happened.

That counter is not cosmetic: it is checked against `maxLoops`, so a graph that
ships through a gate more than once burns retries it never used and can abort a
loop early with "gave up after N tries".

Shape is random seed 19, reproduced verbatim: three chained agents, the FIRST
and LAST feeding the gate (a1 deliberately does not), redo looping back to a0,
and both the ship arm and a2 feeding `z` -- so `z` has already run when the gate
ships to it. Two FAIL verdicts precede the passes.
"""

import pytest

from wfgraph.runner import start_run
from wfgraph.store import load_events, save_documents

pytestmark = pytest.mark.usefixtures("wf_home")


SCENARIO = {
    "steps": [
        {"id": "t", "kind": "trigger", "title": "t"},
        {"id": "a0", "kind": "agent", "title": "a0"},
        {"id": "a1", "kind": "agent", "title": "a1"},
        {"id": "a2", "kind": "agent", "title": "a2"},
        {
            "id": "g",
            "kind": "gate",
            "title": "g",
            "maxLoops": 4,
            "arms": [
                {"id": "redo", "when": {"mode": "any-fail"}},
                {"id": "ship", "when": {"mode": "always"}},
            ],
        },
        {"id": "z", "kind": "agent", "title": "z"},
    ],
    "edges": [
        {"source": "t", "target": "a0"},
        {"source": "a0", "target": "a1"},
        {"source": "a1", "target": "a2"},
        {"source": "a0", "target": "g"},
        {"source": "a2", "target": "g"},
        {"source": "g", "target": "a0", "sourceHandle": "redo", "loop": True},
        {"source": "g", "target": "z", "sourceHandle": "ship"},
        {"source": "a2", "target": "z"},
    ],
}


def _run():
    save_documents(
        [{"id": "budget", "name": "budget", "scenario": SCENARIO}],
        current_id="budget",
    )
    calls = []
    budget = {"n": 2}

    def execute(goal, context, payload, cfg):
        node = str(goal).strip()
        calls.append(node)
        if budget["n"] > 0 and node.startswith("a"):
            budget["n"] -= 1
            return {"ok": True, "text": "x", "verdict": "FAIL"}
        return {"ok": True, "text": "x", "verdict": "PASS"}

    state = start_run("budget", payload={}, background=False, execute_fn=execute)
    return state, calls


def test_one_rework_take_counts_as_one_loop():
    """The gate looped back once, so the run spent exactly one retry."""
    state, calls = _run()

    assert calls.count("a0") == 2, f"expected exactly one rework take, got {calls}"
    assert state["loops"] == 1, (
        f"one rework take, but the run counted {state['loops']} loops -- "
        "shipping forward through the gate was miscounted as rework"
    )


def test_only_the_loop_edge_raises_the_counter():
    """Every LoopAdvanced event points at the declared loop target."""
    state, _ = _run()
    events = load_events(state["runId"])
    advanced = [e for e in events if e["type"] == "LoopAdvanced"]

    assert [e["payload"]["to"] for e in advanced] == ["a0"], (
        "a forward arm raised the loop counter"
    )


def test_the_retry_budget_survives_a_second_ship():
    """maxLoops=4 with one real retry must leave budget to spare."""
    state, _ = _run()

    assert state["status"] == "succeeded", state.get("error")
    assert state["loops"] < 4, "shipping forward ate the retry budget"
