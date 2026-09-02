"""Graphs a person would plausibly draw, validated at start rather than at 3am.

Both of these were found by building one realistic workflow -- trigger, two
parallel agents, verify, gate, rework edge, report -- and running it. Neither
was reachable from the existing suite, which only ever drew graphs already
known to work.
"""
from __future__ import annotations

import pytest

from wfgraph.runner import WorkflowGraphError, start_run
from wfgraph.store import list_runs, save_documents


def _doc(edges, arms=None):
    return {
        "id": "wf",
        "name": "wf",
        "scenario": {
            "steps": [
                {"id": "a", "kind": "agent", "config": {"title": "A", "goal": "a"}},
                {"id": "check", "kind": "agent", "config": {"title": "V", "goal": "v"}},
                {
                    "id": "g",
                    "kind": "gate",
                    "config": {
                        "title": "G",
                        "arms": arms
                        or [
                            {"id": "redo", "when": {"mode": "any-fail"}},
                            {"id": "done", "when": {"mode": "all-pass"}},
                        ],
                    },
                },
                {"id": "report", "kind": "agent", "config": {"title": "R", "goal": "r"}},
            ],
            "edges": edges,
        },
    }


_BASE_EDGES = [
    {"id": "e1", "source": "a", "target": "check"},
    {"id": "e2", "source": "check", "target": "g"},
    {"id": "e4", "source": "g", "target": "report", "sourceHandle": "done"},
]


def test_a_rework_edge_without_the_loop_flag_is_rejected_not_deadlocked(wf_home):
    """A back-edge is a dependency unless it says otherwise.

    Draw gate -> check as a rework path and forget "loop": True, and check
    gains a predecessor that only ever runs after it. Nothing can start it, so
    the run sat until the readiness sweep gave up and reported
    'never became ready' -- a deadlock reported as a node failure, with the
    real cause (one missing flag) nowhere in the message.
    """
    back = {"id": "e3", "source": "g", "target": "check", "sourceHandle": "redo"}
    save_documents([_doc([*_BASE_EDGES, back])], "wf")

    with pytest.raises(WorkflowGraphError) as excinfo:
        start_run("wf", payload=None, source="manual",
                  execute_fn=lambda *a, **k: {"ok": True, "verdict": "PASS"},
                  background=False)

    msg = str(excinfo.value)
    assert "loop" in msg
    assert "check" in msg and "g" in msg


def test_the_same_edge_with_the_loop_flag_is_accepted(wf_home):
    """The fix must reject the missing flag, not the cycle itself."""
    back = {"id": "e3", "source": "g", "target": "check",
            "sourceHandle": "redo", "loop": True}
    save_documents([_doc([*_BASE_EDGES, back])], "wf")

    calls = []

    def ex(goal, ctx, payload, cfg):
        calls.append(str(cfg.get("title")))
        if str(cfg.get("title")) == "V" and calls.count("V") == 1:
            return {"ok": True, "verdict": "FAIL", "summary": "rework"}
        return {"ok": True, "verdict": "PASS", "summary": "ok"}

    state = start_run("wf", payload=None, source="manual", execute_fn=ex,
                      background=False)

    assert state["status"] == "succeeded"
    assert calls.count("V") == 2, "the rework arm should have sent it back once"
    assert "report" in state["ran"]


def test_a_string_when_does_not_crash_the_gate(wf_home):
    """arm["when"] as a bare string is the obvious way to write it by hand.

    _arm_matches assumed a dict and went straight to when.get(...), so a string
    raised AttributeError from inside the gate -- an unhandled crash mid-run,
    not a validation error.

    Asserts rejection happens at start_run, before any step executes: catching
    it only inside the gate would still burn every upstream agent call first.
    """
    save_documents([_doc(_BASE_EDGES, arms=[
        {"id": "redo", "when": "FAIL"},
        {"id": "done", "when": "PASS"},
    ])], "wf")

    ran = []

    def ex(goal, ctx, payload, cfg):
        ran.append(str(cfg.get("title")))
        return {"ok": True, "verdict": "PASS"}

    with pytest.raises(WorkflowGraphError) as excinfo:
        start_run("wf", payload=None, source="manual", execute_fn=ex,
                  background=False)

    assert "when" in str(excinfo.value)
    assert ran == [], "a malformed gate must be caught before any agent runs"
    assert list_runs("wf") == [], "a graph rejected at the door leaves no run behind"
