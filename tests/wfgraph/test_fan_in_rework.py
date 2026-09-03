"""A step waits for its predecessors, even ones marked `satisfied`.

`satisfied` records a step whose PASS still stands across a rework take, so the
loop does not redo work it already accepted. Readiness treats a satisfied
predecessor as finished:

    if all(pred in ran or pred in satisfied or ... for pred in preds(...))

That is right only while the satisfied step is not itself scheduled to run
again. In a fan-in rework loop it is:

    t -> work -> audit -> check(gate) --redo--> work
           \\------------> check          \\-done-> report

On the rework take `audit` is queued again AND listed in `satisfied`. The gate
`check` then became ready in the same pass as `audit` -- reading `audit`'s
take-1 verdict while take-2 was still running beside it. The gate evaluated
twice, so `report` shipped twice on a graph that ships once.

These tests pin the rule: a predecessor that is queued or in flight is not
finished, whatever `satisfied` says.
"""

from __future__ import annotations

import pytest

from wfgraph.runner import start_run
from wfgraph.store import load_events, save_documents

pytestmark = pytest.mark.usefixtures("wf_home")


def _fan_in_rework_graph(max_loops: int = 5) -> dict:
    """work fans out to audit and check; audit also feeds check.

    `check` therefore has two non-loop predecessors, and its redo arm loops
    back to `work` -- so both feeders re-run on a rework take.
    """
    return {
        "id": "fanin",
        "name": "fanin",
        "scenario": {
            "steps": [
                {"id": "t", "kind": "trigger", "title": "t"},
                {"id": "work", "kind": "agent", "title": "work"},
                {"id": "audit", "kind": "agent", "title": "audit"},
                {
                    "id": "check",
                    "kind": "gate",
                    "title": "check",
                    "maxLoops": max_loops,
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
                {"source": "check", "target": "work", "sourceHandle": "redo", "loop": True},
                {"source": "check", "target": "report", "sourceHandle": "done"},
            ],
        },
    }


def _run(doc, verdicts):
    """Walk the graph; `work` pops a verdict per visit so we can force a redo."""
    save_documents([doc], current_id=doc["id"])
    seen: list[str] = []

    def execute(goal, context, payload, cfg):
        node = str(goal).strip()
        seen.append(node)
        if node == "work":
            verdict = verdicts.pop(0) if verdicts else "PASS"
            return {"ok": True, "text": "attempt", "verdict": verdict}
        return {"ok": True, "text": "ok", "verdict": "PASS"}

    state = start_run(doc["id"], payload={}, background=False, execute_fn=execute)
    return state, seen


def test_a_gate_never_runs_beside_the_step_feeding_it():
    """The invariant underneath the bug: a predecessor and its consumer must
    never be in flight together, however the predecessor was marked done."""
    doc = _fan_in_rework_graph()
    save_documents([doc], current_id="fanin")

    import wfgraph.runner as runner_module

    violations: list[tuple[list[str], list[str]]] = []
    original_save = runner_module.save_run

    def watching_save(state):
        in_flight = list(state.get("inFlight") or [])
        if "audit" in in_flight and "check" in in_flight:
            violations.append((in_flight, list(state.get("satisfied") or [])))
        return original_save(state)

    runner_module.save_run = watching_save
    try:
        verdicts = ["FAIL", "PASS"]
        seen: list[str] = []

        def execute(goal, context, payload, cfg):
            node = str(goal).strip()
            seen.append(node)
            if node == "work":
                return {"ok": True, "text": "a", "verdict": verdicts.pop(0) if verdicts else "PASS"}
            return {"ok": True, "text": "ok", "verdict": "PASS"}

        start_run("fanin", payload={}, background=False, execute_fn=execute)
    finally:
        runner_module.save_run = original_save

    assert not violations, (
        f"'check' ran in the same pass as its own predecessor 'audit': {violations}"
    )


def test_a_rework_loop_ships_its_final_step_once():
    """The visible symptom: `report` is downstream of the gate's done arm, so
    it must run exactly once no matter how many takes the loop needed."""
    state, seen = _run(_fan_in_rework_graph(), ["FAIL", "PASS"])

    assert seen.count("report") == 1, seen
    assert state["status"] == "succeeded", state.get("error")


def test_a_fan_in_gate_is_evaluated_once_per_take():
    """Two arms feed the gate, but a take is one decision -- not one per arm.

    One FAIL take plus one PASS take is two evaluations. A third means the
    gate fired twice for a single take.
    """
    state, _ = _run(_fan_in_rework_graph(), ["FAIL", "PASS"])
    evaluations = [e for e in load_events(state["runId"]) if e["type"] == "GateEvaluated"]

    assert len(evaluations) == 2, [e["payload"].get("route") for e in evaluations]


def test_the_plain_rework_loop_still_works():
    """Guard the fix: the single-feeder loop this engine is built for is
    unchanged -- work runs twice, report ships once."""
    doc = _fan_in_rework_graph()
    # drop the audit arm, leaving t -> work -> check -> report
    doc["scenario"]["steps"] = [s for s in doc["scenario"]["steps"] if s["id"] != "audit"]
    doc["scenario"]["edges"] = [
        e
        for e in doc["scenario"]["edges"]
        if "audit" not in (e["source"], e["target"])
    ]

    state, seen = _run(doc, ["FAIL", "PASS"])

    assert seen.count("work") == 2, seen
    assert seen.count("report") == 1, seen
    assert state["loops"] == 1, state["loops"]
    assert state["status"] == "succeeded", state.get("error")


def test_a_satisfied_step_still_gets_skipped_on_rework():
    """The fix must not defeat `satisfied`: a step whose PASS still stands is
    skipped rather than redone, so the loop does not repeat accepted work."""
    state, seen = _run(_fan_in_rework_graph(), ["FAIL", "PASS"])

    skipped = [
        e["payload"]["nodeId"]
        for e in load_events(state["runId"])
        if e["type"] == "NodeSkipped"
    ]

    assert "audit" in skipped, skipped
