"""A wait that does not wait is worse than no wait at all.

park_wait falls back to seconds = 0 for any timer spec it cannot parse, so a
typo'd duration silently turns a deliberate pause into a no-op and the run
sails straight through. "1h" works; "1 hour", "60" and "abc" did not, and
nothing said so. A soak period or a deploy gate written that way looks like it
held and never did.
"""
from __future__ import annotations

import pytest

from wfgraph.runner import WorkflowGraphError, start_run
from wfgraph.store import list_runs, save_documents


def _save(until):
    config = {"title": "W"}
    if until is not None:
        config["until"] = until
    save_documents([{"id": "w", "name": "w", "scenario": {
        "steps": [
            {"id": "a", "kind": "agent", "config": {"title": "A", "goal": "g"}},
            {"id": "pause", "kind": "wait", "config": config},
        ],
        "edges": [{"id": "e1", "source": "a", "target": "pause"}],
    }}], "w")


def _start():
    return start_run("w", payload=None, source="manual",
                     execute_fn=lambda *a, **k: {"ok": True, "verdict": "PASS", "summary": "ok"},
                     background=False)


@pytest.mark.parametrize("spec", ["1 hour", "abc", "60", "an hour", "1hr30"])
def test_an_unparseable_timer_spec_is_rejected(wf_home, spec):
    """Each of these read as zero and skipped the wait."""
    _save({"type": "timer", "spec": spec})

    with pytest.raises(WorkflowGraphError) as excinfo:
        _start()

    msg = str(excinfo.value)
    assert "pause" in msg, "name the offending step"
    assert spec in msg, "and show what it could not parse"
    assert list_runs("w") == []


@pytest.mark.parametrize("spec", ["1h", "30s", "5m", "2d", "1.5h"])
def test_valid_timer_specs_still_park(wf_home, spec):
    """The guard must reject typos, not tighten the durations that worked."""
    _save({"type": "timer", "spec": spec})

    state = _start()

    assert state["status"] == "waiting_world"
    assert state["ran"] == ["a"], "the wait must not have run through"
    assert (state.get("park") or {}).get("kind") == "wait"


def test_an_explicitly_empty_timer_is_still_a_no_op(wf_home):
    """An empty spec is how the engine expresses 'do not actually wait'.

    Distinct from a typo: nothing was written, so nothing was misread. Kept
    working so existing zero-length timers behave as before.
    """
    _save({"type": "timer", "spec": ""})

    state = _start()

    assert state["status"] == "succeeded"
    assert state["ran"] == ["a", "pause"]


def test_a_wait_with_no_until_block_is_still_a_no_op(wf_home):
    """Same reasoning: absent config is not a misparsed duration."""
    _save(None)

    state = _start()

    assert state["status"] == "succeeded"
    assert state["ran"] == ["a", "pause"]


def test_event_and_poll_waits_are_untouched(wf_home):
    """Only timer specs are parsed as durations; other kinds park on the bus."""
    _save({"type": "event", "spec": "github.pull_request.merged"})

    state = _start()

    assert state["status"] == "waiting_world"
    assert state["ran"] == ["a"]
