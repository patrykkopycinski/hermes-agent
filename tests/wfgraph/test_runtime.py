"""runtime.py: the per-run plumbing every other module leans on.

Zero test references before this file, yet runner/waits/fake/tool all import
it. Each test pins a behaviour whose comment says it was a real bug once.
"""

import json

import pytest


@pytest.fixture()
def state(wf_home):
    from wfgraph.store import save_run

    st = {"runId": "r1", "workflowId": "w", "status": "running", "seq": 0}
    save_run(st)
    return st


def test_emit_numbers_events_from_the_caller_not_the_file(state):
    """Re-reading the file to pick a seq races an in-flight save and reuses
    numbers; the canvas then drops every event after the collision."""
    from wfgraph.runtime import emit

    emit(state, "A")
    emit(state, "B")
    emit(state, "C")

    assert state["seq"] == 3
    from wfgraph.store import load_events

    seqs = [e["seq"] for e in load_events("r1")]
    assert seqs == [0, 1, 2], seqs


def test_emit_keeps_numbering_correctly_when_the_stored_run_is_stale(state):
    """The on-disk copy lagging behind must not renumber a live event."""
    from wfgraph.runtime import emit
    from wfgraph.store import save_run

    emit(state, "A")
    save_run({"runId": "r1", "workflowId": "w", "status": "running", "seq": 0})
    emit(state, "B")

    from wfgraph.store import load_events

    assert [e["seq"] for e in load_events("r1")] == [0, 1]


def test_cancel_signal_reaches_the_live_loop_through_memory(state):
    """The runner holds its own state dict and save_run's it, so a flag written
    only to disk gets clobbered before the loop ever sees it."""
    from wfgraph.runtime import absorb_signals, clear_signal, signal

    signal("r1", "cancel")
    absorb_signals(state)
    assert state["status"] == "cancelled"
    clear_signal("r1")


def test_pause_signal_sets_the_pause_flag_not_the_status(state):
    from wfgraph.runtime import absorb_signals, clear_signal, signal

    signal("r1", "pause")
    absorb_signals(state)
    assert state.get("pauseRequested") is True
    assert state["status"] == "running"
    clear_signal("r1")


def test_a_cleared_signal_stops_applying(state):
    from wfgraph.runtime import absorb_signals, clear_signal, signal

    signal("r1", "cancel")
    clear_signal("r1")
    absorb_signals(state)
    assert state["status"] == "running"


def test_signals_do_not_leak_between_runs(state):
    """Keying by run id is the whole point -- a cancel must not stop a sibling."""
    from wfgraph.runtime import absorb_signals, clear_signal, signal

    other = {"runId": "r2", "workflowId": "w", "status": "running", "seq": 0}
    signal("r1", "cancel")
    absorb_signals(other)
    assert other["status"] == "running"
    clear_signal("r1")


def test_fail_dead_run_marks_it_failed_and_says_why(state):
    """A run whose worker is gone would otherwise spin at 'running' forever."""
    from wfgraph.runtime import fail_dead_run

    out = fail_dead_run(state)
    assert out["status"] == "failed"
    assert out["failed"] is True

    from wfgraph.store import load_events

    last = load_events("r1")[-1]
    assert last["type"] == "RunFinished"
    assert "died" in json.dumps(last)


def test_fail_dead_run_clears_a_pending_pause(state):
    """Otherwise the corpse resumes as paused if anything reloads it."""
    from wfgraph.runtime import fail_dead_run

    state["pauseRequested"] = True
    out = fail_dead_run(state)
    assert out["pauseRequested"] is False


def test_thread_alive_is_false_for_a_run_that_was_never_spawned(state):
    from wfgraph.runtime import thread_alive

    assert thread_alive("never-started") is False


def test_lock_for_returns_one_stable_lock_per_run(state):
    from wfgraph.runtime import lock_for

    assert lock_for("r1") is lock_for("r1")
    assert lock_for("r1") is not lock_for("r2")
