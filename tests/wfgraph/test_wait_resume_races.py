"""A wait resumes once, even if two callers both hold a valid-looking park.

`tick_timers` and `tick_polls` read `state["park"]` outside the run lock, then
hand that dict to `_resume`, which re-loads under the lock and checks only:

    if live is None or live.get("status") != "waiting_world":
        return

That is the right question for "is this run still parked" and the wrong one for
"is this still the park I was called for". Two threads can both pass it:

  * a timer thread fires for w1 and blocks on the lock
  * the boot-time sweep (or another tick) resumes w1 legitimately, and the walk
    parks the run again on the next wait
  * the first thread takes the lock, sees `waiting_world` -- true, but of the
    NEW park -- and resolves w1 a second time

The damage is on the record: w1 appended to `ran` twice, two WaitResolved
events for one wait, and (when the second park belongs to a different node) a
live park cleared by a resume nobody asked for, so that wait never elapses.

Reproduced deterministically below by holding one `_resume` at the lock while
the other completes.
"""

import threading
import time

import pytest

import wfgraph.waits as waits
from wfgraph.runner import set_execute_fn, start_run
from wfgraph.store import load_events, load_run, save_documents, save_run

pytestmark = pytest.mark.usefixtures("wf_home")


def _wait_resolved_count(run_id, node_id="w"):
    """How many times the engine says this wait finished."""
    return len(
        [
            event
            for event in load_events(run_id)
            if event.get("type") == "WaitResolved"
            and (event.get("payload") or {}).get("nodeId") == node_id
        ]
    )


TWO_WAITS = {
    "id": "twowaits",
    "name": "twowaits",
    "scenario": {
        "steps": [
            {"id": "t", "kind": "trigger", "title": "t"},
            {
                "id": "w1",
                "kind": "wait",
                "title": "w1",
                "config": {"until": {"type": "timer", "spec": "2m"}},
            },
            {
                "id": "w2",
                "kind": "wait",
                "title": "w2",
                "config": {"until": {"type": "timer", "spec": "1h"}},
            },
            {"id": "tail", "kind": "agent", "title": "tail"},
        ],
        "edges": [
            {"source": "t", "target": "w1"},
            {"source": "w1", "target": "w2"},
            {"source": "w2", "target": "tail"},
        ],
    },
}


def _passing(goal, context, payload, cfg):
    return {"ok": True, "text": "done", "verdict": "PASS"}


def _park_on_first_wait():
    save_documents([TWO_WAITS], current_id="twowaits")
    started = start_run("twowaits", payload={}, background=False, execute_fn=_passing)
    parked = load_run(started["runId"])
    assert (parked.get("park") or {}).get("nodeId") == "w1"
    return parked


class _GatedLock:
    """Holds the first caller at the door until the test says go."""

    def __init__(self, inner, at_door, release):
        self._inner = inner
        self._at_door = at_door
        self._release = release

    def __enter__(self):
        self._at_door.set()
        self._release.wait(10)
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def _race_two_resumes(monkeypatch, parked):
    """Drive the interleaving: stale resume held at the lock, real one runs."""
    run_id = parked["runId"]
    stale_park = dict(parked["park"])

    due = load_run(run_id)
    due["wakeAt"] = time.time() - 1
    save_run(due)

    at_door = threading.Event()
    release = threading.Event()
    original_lock = waits.lock_for
    armed = {"used": False}

    def gated_lock_for(rid):
        if not armed["used"]:
            armed["used"] = True
            return _GatedLock(original_lock(rid), at_door, release)
        return original_lock(rid)

    def stale_resume():
        waits.lock_for = gated_lock_for
        try:
            waits._resume(parked, stale_park, "elapsed")
        finally:
            waits.lock_for = original_lock

    thread = threading.Thread(target=stale_resume, daemon=True)
    thread.start()
    assert at_door.wait(10), "the stale resume never reached the lock"

    waits.lock_for = original_lock
    waits.tick_timers(run_id=run_id)
    time.sleep(0.4)
    midway = load_run(run_id)

    release.set()
    thread.join(10)
    time.sleep(0.4)
    return midway, load_run(run_id)


def test_a_wait_is_not_resolved_twice_by_a_stale_timer(monkeypatch):
    parked = _park_on_first_wait()
    midway, after = _race_two_resumes(monkeypatch, parked)

    assert (midway.get("park") or {}).get("nodeId") == "w2", (
        "the legitimate resume did not move the run on to the second wait"
    )

    ran = after.get("ran") or []
    assert ran.count("w1") == 1, f"w1 was resolved more than once: ran={ran}"


def test_a_stale_timer_does_not_emit_a_second_wait_resolved(monkeypatch):
    parked = _park_on_first_wait()
    _, _ = _race_two_resumes(monkeypatch, parked)

    resolved = [
        event
        for event in load_events(parked["runId"])
        if event.get("type") == "WaitResolved"
        and (event.get("payload") or {}).get("nodeId") == "w1"
    ]
    assert len(resolved) == 1, (
        f"one wait produced {len(resolved)} WaitResolved events"
    )


def test_a_stale_timer_does_not_clear_the_live_park(monkeypatch):
    """The worst shape: the stale resume clears a park it never owned, so the
    second wait never elapses and its successor becomes reachable early."""
    parked = _park_on_first_wait()
    _, after = _race_two_resumes(monkeypatch, parked)

    assert (after.get("park") or {}).get("nodeId") == "w2", (
        "the second wait's park was cleared by a resume for the first wait"
    )
    assert after.get("status") == "waiting_world"


LOOPED_WAIT = {
    "id": "loopwait",
    "name": "loopwait",
    "scenario": {
        "steps": [
            {"id": "t", "kind": "trigger", "title": "t"},
            {
                "id": "w",
                "kind": "wait",
                "title": "w",
                "config": {"until": {"type": "timer", "spec": "2m"}},
            },
            {"id": "a", "kind": "agent", "title": "a"},
            {
                "id": "g",
                "kind": "gate",
                "title": "g",
                "maxLoops": 5,
                "arms": [
                    {"id": "redo", "when": {"mode": "any-fail"}},
                    {"id": "done", "when": {"mode": "always"}},
                ],
            },
            {"id": "out", "kind": "agent", "title": "out"},
        ],
        "edges": [
            {"source": "t", "target": "w"},
            {"source": "w", "target": "a"},
            {"source": "a", "target": "g"},
            {"source": "g", "target": "out", "sourceHandle": "done"},
            {"source": "g", "target": "w", "sourceHandle": "redo", "loop": True},
        ],
    },
}


def test_a_stale_take_does_not_resolve_the_same_node_on_a_later_take():
    """A wait inside a rework loop parks on the same node more than once.

    Node identity alone cannot tell take 0 from take 1, so a timer armed for
    the first visit would resolve the second one -- skipping a wait the rework
    was supposed to sit through. The take is the other half of the identity.
    """
    save_documents([LOOPED_WAIT], current_id="loopwait")
    verdicts = iter(["FAIL", "PASS", "PASS", "PASS"])

    def execute(goal, context, payload, cfg):
        return {"ok": True, "text": "done", "verdict": next(verdicts, "PASS")}

    # Resumes spawn without an execute_fn, so register it process-wide.
    set_execute_fn(execute)
    try:
        started = start_run("loopwait", payload={}, background=False, execute_fn=execute)
        run_id = started["runId"]
        first_park = dict(load_run(run_id).get("park") or {})
        assert first_park.get("nodeId") == "w"
        assert int(first_park.get("iteration") or 0) == 0

        due = load_run(run_id)
        due["wakeAt"] = time.time() - 1
        save_run(due)
        waits.tick_timers(run_id=run_id)
        time.sleep(0.6)

        reparked = load_run(run_id)
        live_park = reparked.get("park") or {}
        assert live_park.get("nodeId") == "w", "the rework did not re-park on the wait"
        assert int(live_park.get("iteration") or 0) == 1, "the re-park kept the old take"

        resolved_before = _wait_resolved_count(run_id)

        # The take-0 timer fires late against the take-1 park.
        waits._resume(reparked, first_park, "elapsed")
        time.sleep(0.5)

        assert _wait_resolved_count(run_id) == resolved_before, (
            "a timer armed for take 0 resolved the take 1 park"
        )
        after = load_run(run_id)
        assert (after.get("park") or {}).get("nodeId") == "w", (
            "the second visit's wait was cleared by the first visit's timer"
        )
    finally:
        set_execute_fn(None)


def test_a_normal_timer_resume_still_works():
    """Guard rail: the fix must not stop a legitimate resume."""
    parked = _park_on_first_wait()
    run_id = parked["runId"]

    due = load_run(run_id)
    due["wakeAt"] = time.time() - 1
    save_run(due)

    waits.tick_timers(run_id=run_id)
    time.sleep(0.4)

    after = load_run(run_id)
    assert "w1" in (after.get("ran") or []), "the wait never resumed"
    assert (after.get("park") or {}).get("nodeId") == "w2", (
        "the run did not move on to the next wait"
    )
