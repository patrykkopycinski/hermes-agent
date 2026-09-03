"""F1 regression: a gate must never route on an arm it cannot resolve.

The bug these bite: `succs(scenario, node, handle)` ignores its filter when
handle is None, so an arm without an id matched every outgoing edge and the
gate took `targets[0]` — walking the PASS branch on a FAIL verdict, exiting
clean with loops: 0.
"""
from __future__ import annotations

import pytest

from wfgraph.runner import WorkflowGraphError, start_run


def _scenario(*, arm_id):
    """verify -> gate -{fail}-> rework, -{pass}-> report.

    One arm, firing on any upstream FAIL. `arm_id` is the sourceHandle it
    claims; the rework edge is labelled "fail".
    """
    arm = {"when": {"mode": "any-fail"}}
    if arm_id is not None:
        arm["id"] = arm_id
    return {
        "steps": [
            {"id": "verify", "kind": "agent", "title": "Verify", "config": {"prompt": "check"}},
            {"id": "gate", "kind": "gate", "title": "Gate", "config": {"arms": [arm]}},
            {"id": "rework", "kind": "agent", "title": "Rework", "config": {"prompt": "fix"}},
            {"id": "report", "kind": "agent", "title": "Report", "config": {"prompt": "write"}},
        ],
        "edges": [
            {"source": "verify", "target": "gate"},
            {"source": "gate", "target": "rework", "sourceHandle": "fail"},
            {"source": "gate", "target": "report", "sourceHandle": "pass"},
        ],
    }


def _failing_agent(prompt, context, payload, cfg):
    return {"ok": True, "verdict": "FAIL", "summary": "it failed"}


def test_arm_without_id_is_rejected_not_guessed(wf_home):
    """An unlabelled arm must raise, not match every edge and take the first."""
    with pytest.raises(WorkflowGraphError) as exc:
        start_run(
            "f1_noid",
            scenario=_scenario(arm_id=None),
            background=False,
            execute_fn=_failing_agent,
        )
    assert "arm with no id" in str(exc.value)


def test_arm_pointing_at_no_edge_is_rejected(wf_home):
    """A matched arm whose handle labels no edge must raise, not fall through.

    Caught at validation now (`reject_unrouted_gate_arms`) rather than when
    the gate is reached, so the message is the validator's. Asserted on the
    parts that carry the diagnosis -- which arm, and that it is unrouted --
    rather than one phrasing, so the check can move without a false failure.
    """
    with pytest.raises(WorkflowGraphError) as exc:
        start_run(
            "f1_dangling",
            scenario=_scenario(arm_id="nonexistent"),
            background=False,
            execute_fn=_failing_agent,
        )
    msg = str(exc.value)
    assert "nonexistent" in msg
    assert "sourceHandle" in msg


def test_fail_verdict_cannot_reach_the_pass_branch(wf_home):
    """The behavioural bite: FAIL routes to rework, never to report."""
    state = start_run(
        "f1_routes",
        scenario=_scenario(arm_id="fail"),
        background=False,
        execute_fn=_failing_agent,
    )
    assert "rework" in state["ran"], state["ran"]
    assert "report" not in state["ran"], state["ran"]
