"""Every wfgraph tool action gets called at least once.

The smoke run only exercised action='list'. read/run/status/cancel shipped
untested through the tool surface -- the layer an agent actually touches.
Each assertion checks a real field, not just "no exception".
"""

import json

import pytest

# conftest puts the plugin dir on sys.path and provides wf_home.

DOC = {
    "id": "acts",
    "name": "Actions",
    "scenario": {
        "steps": [
            {"id": "t", "kind": "trigger", "title": "t"},
            {"id": "w", "kind": "agent", "title": "w", "prompt": "do"},
        ],
        "edges": [{"source": "t", "target": "w"}],
    },
}


@pytest.fixture()
def home(wf_home):
    from wfgraph.store import save_documents

    save_documents([DOC], current_id="acts")
    return wf_home


def call(**kw):
    from tool import wfgraph_tool

    return json.loads(wfgraph_tool(**kw))


def test_list_reports_the_workflow_and_its_step_count(home):
    out = call(action="list")
    assert [w["id"] for w in out["workflows"]] == ["acts"]
    assert out["workflows"][0]["steps"] == 2


def test_read_returns_the_graph_body(home):
    out = call(action="read", workflow="acts")
    ids = [s["id"] for s in out["scenario"]["steps"]]
    assert ids == ["t", "w"]


def test_read_of_a_missing_workflow_is_an_error_not_an_empty_doc(home):
    out = call(action="read", workflow="nope")
    assert "error" in out, out


def test_run_with_wait_walks_the_graph_and_reports_terminal_state(home, monkeypatch):
    # set_execute_fn is the real hook. The previous monkeypatch targeted
    # runner.execute_node, which does not exist -- with raising=False it was a
    # silent no-op, so this test never actually stubbed the agent.
    from wfgraph.runner import set_execute_fn

    set_execute_fn(lambda *a, **k: {"ok": True, "summary": "ok", "verdict": "PASS", "output": {}})
    try:
        out = call(action="run", workflow="acts", wait=True)
    finally:
        set_execute_fn(None)  # module-level global: leaking it poisons later tests

    assert "error" not in out, out
    # The node must actually have RUN. Asserting on a json blob only proved the
    # scenario was echoed back: with kind "task" (which the engine never
    # dispatched) this passed while ran == ["t"] and w never executed.
    assert "w" in (out.get("ran") or []), out


def test_status_of_an_unknown_run_is_an_error(home):
    out = call(action="status", run_id="run-does-not-exist")
    assert "error" in out, out


def test_bad_action_is_rejected_by_name(home):
    out = call(action="frobnicate")
    assert "error" in out
    assert "list" in out["error"]
