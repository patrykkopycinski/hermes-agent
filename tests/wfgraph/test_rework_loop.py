"""The rework loop -- the one thing Kanban structurally cannot do.

kanban_db._would_cycle rejects any link that closes a cycle
("linking X -> Y would create a cycle"), so a DAG cannot express
"verify failed, go fix it, come back and check again". That capability
is the whole argument for a second execution model, so it gets pinned:
it must iterate, and it must terminate.

Deterministic -- a stub executor supplies the verdicts, no model spend.
"""


def _graph(max_loops=5):
    """work -> check -(any-fail)-> work, with a report on the always arm.

    Arm order matters: the runner takes the FIRST matching arm, so the
    failure arm must precede the always/catch-all arm.
    """
    return {
        "id": "loop",
        "name": "Loop",
        "scenario": {
            "steps": [
                {"id": "t", "kind": "trigger", "title": "t"},
                {"id": "work", "kind": "agent", "title": "work"},
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
                {"source": "work", "target": "check"},
                {"source": "check", "target": "work", "sourceHandle": "redo", "loop": True},
                {"source": "check", "target": "report", "sourceHandle": "done"},
            ],
        },
    }


def _run(doc, verdicts):
    """Walk the graph with a stub executor. verdicts pop per 'work' visit."""
    from wfgraph.runner import start_run
    from wfgraph.store import save_documents

    save_documents([doc], current_id=doc["id"])
    seen = []

    def execute(goal, context, payload, cfg):
        node = str(goal).strip()
        seen.append(node)
        if node == "work":
            verdict = verdicts.pop(0) if verdicts else "PASS"
            return {"ok": True, "text": "attempt", "verdict": verdict}
        return {"ok": True, "text": "ok", "verdict": "PASS"}

    state = start_run(doc["id"], execute_fn=execute, background=False)
    return state, seen


def test_a_failed_check_sends_work_back_and_it_runs_again(wf_home):
    """The defining behaviour: fail once, and 'work' is visited twice."""
    state, seen = _run(_graph(), ["FAIL", "PASS"])

    assert seen.count("work") == 2, seen
    assert state["loops"] == 1, state["loops"]
    assert "report" in seen, seen
    assert state["status"] == "succeeded", state["status"]


def test_a_passing_check_never_loops(wf_home):
    state, seen = _run(_graph(), ["PASS"])

    assert seen.count("work") == 1, seen
    assert state["loops"] == 0
    assert "report" in seen


def test_repeated_failure_stops_at_the_cap_instead_of_spinning_forever(wf_home):
    """An uncapped cycle is an infinite loop burning real model spend."""
    state, seen = _run(_graph(max_loops=3), ["FAIL"] * 50)

    assert state["loops"] == 3, state["loops"]
    assert seen.count("work") == 4, seen
    assert state["failed"] is True
    assert state["status"] == "failed"


def test_the_cap_is_configurable_per_graph(wf_home):
    state, seen = _run(_graph(max_loops=1), ["FAIL"] * 50)

    assert state["loops"] == 1, state["loops"]
    assert seen.count("work") == 2, seen
