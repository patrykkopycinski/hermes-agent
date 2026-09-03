"""A rework loop's body is every path through it, not the first one found.

``topology.between`` collects the steps that sit between a gate's loop target
and the gate itself. The runner uses that list to decide which already-PASSed
steps count as ``satisfied`` and can be skipped on the next take
(``runner._compute_gate``). A step missing from the body never gets marked
satisfied, so it re-executes work the loop never asked it to redo.

The original walk was ``any(walk(t, ...) for t in succs(...))``. ``any``
short-circuits, so on a fan-out only the arm that happened to be walked first
entered the body -- two structurally identical arms got different treatment,
decided by edge insertion order.

The same walk was also unmemoized: stacked diamonds are 2**n distinct paths
over a graph of 4n+2 nodes, so a hand-authorable canvas hung the engine.
"""

from __future__ import annotations

import time

import pytest

from wfgraph.runner import start_run
from wfgraph.store import load_events, load_run, save_documents
from wfgraph.topology import between

pytestmark = pytest.mark.usefixtures("wf_home")


def _fanout_scenario() -> dict:
    """gate -> {left, right} -> join: both arms are inside the loop body."""
    return {
        "steps": [
            {"id": "gate", "kind": "gate"},
            {"id": "left", "kind": "agent"},
            {"id": "right", "kind": "agent"},
            {"id": "join", "kind": "agent"},
        ],
        "edges": [
            {"source": "gate", "target": "left"},
            {"source": "gate", "target": "right"},
            {"source": "left", "target": "join"},
            {"source": "right", "target": "join"},
        ],
    }


def test_every_arm_of_a_fanout_is_in_the_body():
    body = set(between(_fanout_scenario(), "gate", "join"))
    assert body == {"gate", "left", "right", "join"}


def test_arm_order_does_not_change_the_body():
    """Two identical arms must not be treated differently by edge order."""
    scenario = _fanout_scenario()
    reversed_edges = dict(scenario)
    reversed_edges["edges"] = list(reversed(scenario["edges"]))

    assert set(between(scenario, "gate", "join")) == set(
        between(reversed_edges, "gate", "join")
    )


def test_second_route_into_a_shared_tail_is_kept():
    """A converging path must enter the body even when the tail is memoized.

    ``s -> a -> shared -> e`` and ``s -> b -> shared``. Walking ``a`` first
    caches ``shared`` as reaching ``e``. When ``b`` reaches the memoized
    ``shared``, the cached answer is returned -- but ``b``'s own prefix is
    still new to the body and has to be folded in, or the second arm silently
    vanishes from the loop body.
    """
    scenario = {
        "steps": [
            {"id": "s", "kind": "trigger"},
            {"id": "a", "kind": "agent"},
            {"id": "b", "kind": "agent"},
            {"id": "shared", "kind": "agent"},
            {"id": "e", "kind": "agent"},
        ],
        "edges": [
            {"source": "s", "target": "a"},
            {"source": "s", "target": "b"},
            {"source": "a", "target": "shared"},
            {"source": "b", "target": "shared"},
            {"source": "shared", "target": "e"},
        ],
    }

    assert set(between(scenario, "s", "e")) == {"s", "a", "b", "shared", "e"}


def test_a_cyclic_graph_terminates():
    """wfgraph's whole point is loops, so `between` must survive a cycle.

    ``a -> b -> c -> a`` with ``c -> e`` as the exit. Without the guard that
    refuses to re-enter a node already on the current path, the walk recurses
    until Python gives up -- which is a crash, not a wrong answer.
    """
    scenario = {
        "steps": [{"id": i, "kind": "agent"} for i in ("a", "b", "c", "e")],
        "edges": [
            {"source": "a", "target": "b"},
            {"source": "b", "target": "c"},
            {"source": "c", "target": "a"},
            {"source": "c", "target": "e"},
        ],
    }

    assert set(between(scenario, "a", "e")) == {"a", "b", "c", "e"}


def test_dead_end_branches_stay_out_of_the_body():
    """Collecting every path must not sweep in steps that never reach the end."""
    scenario = {
        "steps": [
            {"id": "gate", "kind": "gate"},
            {"id": "live", "kind": "agent"},
            {"id": "dead", "kind": "agent"},
            {"id": "join", "kind": "agent"},
        ],
        "edges": [
            {"source": "gate", "target": "live"},
            {"source": "gate", "target": "dead"},
            {"source": "live", "target": "join"},
        ],
    }

    body = set(between(scenario, "gate", "join"))

    assert body == {"gate", "live", "join"}
    assert "dead" not in body


def test_wide_graph_does_not_blow_up():
    """Stacked diamonds: 2**18 paths over 56 nodes must stay instant.

    Unmemoized path enumeration took ~4.6s here and ~21s one diamond later.
    """
    steps: list[dict] = [{"id": "s", "kind": "trigger"}]
    edges: list[dict] = []
    prev = "s"
    for i in range(18):
        left, right, join = f"l{i}", f"r{i}", f"j{i}"
        steps += [
            {"id": left, "kind": "agent"},
            {"id": right, "kind": "agent"},
            {"id": join, "kind": "agent"},
        ]
        edges += [
            {"source": prev, "target": left},
            {"source": prev, "target": right},
            {"source": left, "target": join},
            {"source": right, "target": join},
        ]
        prev = join
    steps.append({"id": "e", "kind": "agent"})
    edges.append({"source": prev, "target": "e"})
    scenario = {"steps": steps, "edges": edges}

    started = time.monotonic()
    body = between(scenario, "s", "e")
    elapsed = time.monotonic() - started

    # Every node is genuinely on a path from s to e.
    assert len(body) == len(steps)
    assert elapsed < 1.0, f"between() took {elapsed:.2f}s on a 56-node graph"


def _rework_doc() -> dict:
    """Two identical arms plus a flaky third that forces exactly one redo."""
    return {
        "id": "sat",
        "name": "Sat",
        "scenario": {
            "steps": [
                {"id": "t", "kind": "trigger", "title": "t"},
                {"id": "src", "kind": "agent", "title": "src"},
                {"id": "left", "kind": "agent", "title": "left"},
                {"id": "right", "kind": "agent", "title": "right"},
                {"id": "flaky", "kind": "agent", "title": "flaky"},
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
                {"source": "t", "target": "src"},
                {"source": "src", "target": "left"},
                {"source": "src", "target": "right"},
                {"source": "src", "target": "flaky"},
                {"source": "left", "target": "check"},
                {"source": "right", "target": "check"},
                {"source": "flaky", "target": "check"},
                {"source": "check", "target": "src", "sourceHandle": "redo", "loop": True},
                {"source": "check", "target": "report", "sourceHandle": "done"},
            ],
        },
    }


def test_both_arms_are_satisfied_on_a_rework_take():
    """The live symptom: identical arms must get identical skip treatment."""
    doc = _rework_doc()
    save_documents([doc], current_id=doc["id"])
    verdicts = ["FAIL", "PASS", "PASS", "PASS"]

    def execute(goal, context, payload, cfg=None, **kw):
        node = str(goal).strip()
        if node == "flaky":
            verdict = verdicts.pop(0) if verdicts else "PASS"
            return {"ok": True, "summary": f"flaky {verdict}", "verdict": verdict}
        return {"ok": True, "summary": f"{node} ok", "verdict": "PASS"}

    out = start_run("sat", payload={}, background=False, execute_fn=execute)
    run = load_run(out["runId"])

    assert run["loops"] >= 1, "the rework loop never fired; test proves nothing"

    skipped = {
        event.get("payload", {}).get("nodeId")
        for event in load_events(out["runId"])
        if event.get("type") == "NodeSkipped"
    }
    assert {"left", "right"} <= skipped, (
        f"identical fan-out arms treated differently: skipped={sorted(skipped)}"
    )
    assert {"left", "right"} <= set(run.get("satisfied") or [])
