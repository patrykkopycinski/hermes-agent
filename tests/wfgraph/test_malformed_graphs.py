"""Graph shapes that reported success while doing nothing sane.

Found by feeding the engine the malformed graphs a person or a buggy editor
actually produces. All three reported "succeeded", which is the worst possible
answer: a cron job wired to one of these would report green forever.
"""
from __future__ import annotations

import pytest

from wfgraph.runner import WorkflowGraphError, start_run
from wfgraph.store import list_runs, save_documents


def _run(scenario):
    save_documents([{"id": "w", "name": "w", "scenario": scenario}], "w")
    return start_run(
        "w", payload=None, source="manual",
        execute_fn=lambda g, c, p, cfg: {"ok": True, "verdict": "PASS", "summary": "ok"},
        background=False,
    )


def test_an_empty_scenario_is_rejected_not_reported_as_success(wf_home):
    """A workflow with no steps ran to "succeeded" with ran: [].

    Nothing happened, and the run said it went fine. Anything watching run
    status - a cron summary, a dashboard - reads that as a healthy workflow.
    """
    with pytest.raises(WorkflowGraphError) as excinfo:
        _run({"steps": [], "edges": []})

    assert "no steps" in str(excinfo.value).lower()
    assert list_runs("w") == []


def test_a_step_without_an_id_is_rejected(wf_home):
    """A step missing "id" was dropped on the floor.

    The graph had one agent in it; the run executed nothing and called that
    success. Silently discarding an authored step is never the right read.
    """
    with pytest.raises(WorkflowGraphError) as excinfo:
        _run({"steps": [{"kind": "agent", "config": {"title": "A", "goal": "g"}}],
              "edges": []})

    assert "id" in str(excinfo.value)
    assert list_runs("w") == []


def test_duplicate_step_ids_are_rejected(wf_home):
    """Two steps sharing an id executed twice under one name.

    ran came back as ['a', 'a']: every per-node structure the engine keeps -
    sessions, tries, summaries - is keyed by node id, so the second step
    silently overwrites the first's state as it goes.
    """
    with pytest.raises(WorkflowGraphError) as excinfo:
        _run({"steps": [
            {"id": "a", "kind": "agent", "config": {"title": "A", "goal": "g"}},
            {"id": "a", "kind": "agent", "config": {"title": "A2", "goal": "g"}},
        ], "edges": []})

    msg = str(excinfo.value)
    assert "duplicate" in msg.lower() and "'a'" in msg
    assert list_runs("w") == []


def test_an_edge_pointing_at_a_missing_node_is_rejected(wf_home):
    """An edge to a nonexistent target was ignored rather than questioned.

    Lower severity than the others - the run did execute its real step - but it
    means a typo'd target silently drops a branch of the workflow, and the run
    still reports success.
    """
    with pytest.raises(WorkflowGraphError) as excinfo:
        _run({"steps": [{"id": "a", "kind": "agent", "config": {"title": "A", "goal": "g"}}],
              "edges": [{"id": "e", "source": "a", "target": "ghost"}]})

    assert "ghost" in str(excinfo.value)
    assert list_runs("w") == []


def test_a_valid_graph_still_runs(wf_home):
    """The guards must reject malformed graphs, not tighten the good ones."""
    state = _run({
        "steps": [
            {"id": "a", "kind": "agent", "config": {"title": "A", "goal": "g"}},
            {"id": "b", "kind": "agent", "config": {"title": "B", "goal": "g"}},
        ],
        "edges": [{"id": "e", "source": "a", "target": "b"}],
    })
    assert state["status"] == "succeeded"
    assert state["ran"] == ["a", "b"]
