"""What the engine does when things go wrong.

Unit tests with obedient stubs only ever prove the happy path. These pin the
two ways a run could previously end up lying about itself: a provider blowing
up on the durable (inline) trigger path left the run file at "running" forever,
and a step with a kind nobody handles reported "succeeded" having executed
nothing at all.
"""

import pytest

from wfgraph.runner import WorkflowGraphError, start_run
from wfgraph.store import list_runs, load_run, save_documents


def _scenario(*steps, edges=None):
    return {"steps": list(steps), "edges": list(edges or [])}


def _put(monkeypatch, tmp_path, doc):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    save_documents([doc], doc["id"])


def _agent_doc(workflow_id="wf"):
    return {
        "id": workflow_id,
        "name": workflow_id,
        "scenario": _scenario(
            {"id": "work", "kind": "agent", "config": {"title": "Work", "goal": "go"}},
        ),
    }


def _boom(*_args, **_kwargs):
    raise RuntimeError("provider exploded")


# --- FR-003: a crash must leave the run consistent on disk -----------------

def test_a_provider_crash_on_the_inline_path_marks_the_run_failed(tmp_path, monkeypatch):
    """background=False is the durable trigger path (cron, webhook). An
    exception there used to propagate out with the run file still at
    "running" — a workflow that is neither progressing nor finished."""
    _put(monkeypatch, tmp_path, _agent_doc())

    with pytest.raises(RuntimeError, match="provider exploded"):
        start_run("wf", payload=None, source="cron", execute_fn=_boom, background=False)

    rows = list_runs("wf")
    assert len(rows) == 1
    state = load_run(rows[0]["runId"])
    assert state["status"] == "failed", "a crashed run must not stay 'running'"
    assert state["failed"] is True


def test_a_crashed_run_does_not_block_the_next_trigger(tmp_path, monkeypatch):
    """The consequence that matters operationally: the next cron tick must be
    able to start a fresh run rather than tripping over a stuck one."""
    _put(monkeypatch, tmp_path, _agent_doc())

    with pytest.raises(RuntimeError):
        start_run("wf", payload=None, source="cron", execute_fn=_boom, background=False)

    ok = start_run(
        "wf", payload=None, source="cron",
        execute_fn=lambda *a, **k: {"ok": True, "verdict": "PASS"},
        background=False,
    )
    assert ok["status"] == "succeeded"
    assert ok["ran"] == ["work"]


def test_a_crash_emits_a_run_finished_event(tmp_path, monkeypatch):
    """Anything watching the event stream must see the run end, not hang."""
    from wfgraph.store import list_runs, load_events

    _put(monkeypatch, tmp_path, _agent_doc())
    with pytest.raises(RuntimeError):
        start_run("wf", payload=None, source="cron", execute_fn=_boom, background=False)

    run_id = list_runs("wf")[0]["runId"]
    kinds = [e.get("type") for e in load_events(run_id)]
    assert "RunFinished" in kinds


# --- FR-004: an unknown step kind must fail loudly -------------------------

def test_an_unknown_step_kind_is_rejected(tmp_path, monkeypatch):
    """A typo'd kind used to be skipped silently: no successors queued, and
    the run reported 'succeeded' with ran == []. A green run that did nothing
    is worse than a failure."""
    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "typo",
            "name": "typo",
            "scenario": _scenario(
                {"id": "work", "kind": "banana", "config": {"title": "Work"}},
            ),
        },
    )

    with pytest.raises(WorkflowGraphError) as err:
        start_run("typo", payload=None, source="manual", background=False)

    message = str(err.value)
    assert "banana" in message
    assert "work" in message


def test_every_supported_kind_still_starts(tmp_path, monkeypatch):
    """The guard must not reject the kinds the engine actually implements."""
    _put(
        monkeypatch,
        tmp_path,
        {
            "id": "allkinds",
            "name": "allkinds",
            "scenario": _scenario(
                {"id": "t", "kind": "trigger", "config": {"title": "T", "on": {"type": "manual", "spec": ""}}},
                {"id": "a", "kind": "agent", "config": {"title": "A", "goal": "go"}},
                {"id": "g", "kind": "gate", "config": {"title": "G"}},
                {"id": "h", "kind": "human", "config": {"title": "H"}},
                {"id": "w", "kind": "wait", "config": {"title": "W", "for": "30s"}},
                edges=[{"id": "e1", "source": "t", "target": "a"}],
            ),
        },
    )

    state = start_run(
        "allkinds", payload=None, source="manual",
        execute_fn=lambda *a, **k: {"ok": True, "verdict": "PASS"},
        background=False,
    )
    # The point is only that the guard accepted every implemented kind and the
    # run got going — not the routing, which other tests cover.
    assert state["status"] in {"succeeded", "running", "waiting", "waiting_human", "paused"}
    assert state["ran"], "a graph of valid kinds must actually execute something"
