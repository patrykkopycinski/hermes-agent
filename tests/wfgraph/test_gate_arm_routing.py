"""A gate arm that goes nowhere is caught before the run spends money.

`_run_gate` already refuses to act on an unrouted arm, with a good message.
But it raises *mid-run* -- after every upstream agent step has already
executed, which on the real path means real model calls and real minutes. The
graph was unroutable the moment it was written; there is no reason to learn
that on the far side of the expensive part.

These tests pin the check at `start_run`, where a typo costs nothing.
"""

from __future__ import annotations

import pytest

from wfgraph.runner import start_run
from wfgraph.store import save_documents
from wfgraph.validate import WorkflowGraphError


def _gate_doc(edges: list[dict], arms: list[dict] | None = None) -> dict:
    return {
        "id": "g-doc",
        "name": "gate doc",
        "scenario": {
            "steps": [
                {"id": "t", "kind": "trigger", "config": {}},
                {"id": "chk", "kind": "agent", "config": {"title": "Check", "goal": "check"}},
                {
                    "id": "g",
                    "kind": "gate",
                    "config": {
                        "arms": arms
                        if arms is not None
                        else [
                            {"id": "pass", "when": {"mode": "all-pass"}},
                            {"id": "fail", "when": {"mode": "any-fail"}},
                        ]
                    },
                },
                {"id": "ship", "kind": "agent", "config": {"title": "Ship", "goal": "ship"}},
                {"id": "redo", "kind": "agent", "config": {"title": "Redo", "goal": "redo"}},
            ],
            "edges": edges,
        },
    }


def _never_runs(goal, context=None, payload=None, cfg=None):
    raise AssertionError(
        "an unroutable gate must be rejected before any step executes"
    )


_WIRED = [
    {"id": "e1", "source": "t", "target": "chk"},
    {"id": "e2", "source": "chk", "target": "g"},
    {"id": "e3", "source": "g", "target": "ship", "sourceHandle": "pass"},
    {"id": "e4", "source": "g", "target": "redo", "sourceHandle": "fail"},
]


def test_a_misspelled_gate_handle_is_refused_at_start(wf_home):
    """The exact typo that used to cost a full upstream run to discover."""
    edges = [dict(e) for e in _WIRED]
    edges[2]["sourceHandle"] = "passed"  # arm is 'pass'
    save_documents([_gate_doc(edges)], current_id="g-doc")

    with pytest.raises(WorkflowGraphError) as err:
        start_run("g-doc", source="manual", background=False, execute_fn=_never_runs)

    msg = str(err.value)
    assert "pass" in msg
    assert "g" in msg


def test_an_arm_with_no_edge_at_all_is_refused(wf_home):
    """Arm declared, never connected -- the decision could not be acted on."""
    edges = [e for e in _WIRED if e.get("sourceHandle") != "fail"]
    save_documents([_gate_doc(edges)], current_id="g-doc")

    with pytest.raises(WorkflowGraphError) as err:
        start_run("g-doc", source="manual", background=False, execute_fn=_never_runs)

    assert "fail" in str(err.value)


def test_a_fully_wired_gate_still_starts(wf_home):
    """The guard must not reject the graphs people actually draw."""
    save_documents([_gate_doc([dict(e) for e in _WIRED])], current_id="g-doc")

    def ok(goal, context=None, payload=None, cfg=None):
        return {"summary": "fine", "verdict": "PASS", "output": {}}

    state = start_run("g-doc", source="manual", background=False, execute_fn=ok)
    assert "ship" in state["ran"]


def test_an_arm_with_no_id_is_still_rejected_at_run_time(wf_home):
    """Not this check's job -- `_run_gate` owns it, and already raises.

    Pinned here so moving the routing check earlier does not accidentally
    swallow the unlabelled-arm case: an arm with no id cannot be matched
    against handles at all, so this guard must leave it to the runner.
    """
    doc = _gate_doc(
        [
            {"id": "e1", "source": "t", "target": "chk"},
            {"id": "e2", "source": "chk", "target": "g"},
            {"id": "e3", "source": "g", "target": "ship"},
        ],
        arms=[{"when": {"mode": "all-pass"}}],
    )
    save_documents([doc], current_id="g-doc")

    def ok(goal, context=None, payload=None, cfg=None):
        return {"summary": "fine", "verdict": "PASS", "output": {}}

    with pytest.raises(WorkflowGraphError) as err:
        start_run("g-doc", source="manual", background=False, execute_fn=ok)
    assert "arm with no id" in str(err.value)


def test_a_gate_with_no_arms_is_left_alone(wf_home):
    """No arms means no claim about routing; other checks own that case."""
    doc = _gate_doc(
        [
            {"id": "e1", "source": "t", "target": "chk"},
            {"id": "e2", "source": "chk", "target": "g"},
            {"id": "e3", "source": "g", "target": "ship"},
        ],
        arms=[],
    )
    save_documents([doc], current_id="g-doc")

    def ok(goal, context=None, payload=None, cfg=None):
        return {"summary": "fine", "verdict": "PASS", "output": {}}

    # Must not raise from the arm-routing check.
    start_run("g-doc", source="manual", background=False, execute_fn=ok)
