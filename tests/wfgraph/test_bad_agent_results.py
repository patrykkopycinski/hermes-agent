"""What the engine does when the agent misbehaves.

execute_fn reaches a live model in production. Models and the code around them
return junk sometimes - None on a swallowed timeout, a bare string from a
mis-wired provider shim. The run correctly ends up failed either way; the
question here is whether the failure says anything useful.
"""
from __future__ import annotations

import pytest

from wfgraph.runner import start_run
from wfgraph.store import list_runs, load_run, save_documents


def _save():
    save_documents([{"id": "w", "name": "w", "scenario": {
        "steps": [{"id": "a", "kind": "agent", "config": {"title": "A", "goal": "g"}}],
        "edges": [],
    }}], "w")


@pytest.mark.parametrize("junk", [None, "a string", 42, ["a", "list"]])
def test_a_non_dict_agent_result_fails_with_a_message_that_names_the_cause(wf_home, junk):
    """It used to surface as AttributeError: 'NoneType' object has no attribute 'get'.

    That is a traceback from the middle of the runner pointing at engine
    internals, for what is entirely an agent-side fault. Whoever is on call
    reads it as "the workflow engine crashed" rather than "your agent returned
    None".
    """
    _save()

    with pytest.raises(TypeError) as excinfo:
        start_run("w", payload=None, source="manual",
                  execute_fn=lambda *a, **k: junk, background=False)

    msg = str(excinfo.value)
    assert "'a'" in msg, "the message must name the step that misbehaved"
    assert type(junk).__name__ in msg, "and what it actually returned"


def test_the_run_is_still_marked_failed_not_left_running(wf_home):
    """Diagnosability must not come at the cost of the durable-state fix."""
    _save()

    with pytest.raises(TypeError):
        start_run("w", payload=None, source="manual",
                  execute_fn=lambda *a, **k: None, background=False)

    rows = list_runs("w")
    assert len(rows) == 1
    state = load_run(rows[0]["runId"])
    assert state["status"] == "failed"
    assert state.get("failed") is True


def test_an_empty_dict_is_still_accepted(wf_home):
    """{} is a legal, if uninformative, result - defaults apply. Not an error."""
    _save()

    state = start_run("w", payload=None, source="manual",
                      execute_fn=lambda *a, **k: {}, background=False)

    assert state["status"] == "succeeded"
    assert state["ran"] == ["a"]
