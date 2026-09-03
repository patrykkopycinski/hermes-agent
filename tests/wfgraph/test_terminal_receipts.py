"""A run that ends outside the normal walk is still a finished run.

`runner._attach_receipt` covers a graph that finishes on its own. Two other
paths end a run: the owner process dying (`runtime.fail_dead_run`) and someone
stopping it (`runner.cancel_run`). Both used to leave the record with no
receipt, so a reader could not tell when the run ended or whether any step had
produced anything -- the same false-green gap the receipt was added to close.

`cancel_run` additionally emitted `RunFinished {state: "failed"}` while writing
`status: "cancelled"` to the record. Anything reading the event stream (canvas,
log tail, downstream automation) called a deliberate stop a failure.
"""

from __future__ import annotations

import pytest

from wfgraph.runner import cancel_run
from wfgraph.runtime import fail_dead_run
from wfgraph.store import load_events, load_run, save_run

SCENARIO = {
    "id": "s",
    "steps": [{"id": "a", "kind": "agent"}, {"id": "b", "kind": "agent"}],
    "edges": [{"source": "a", "target": "b"}],
}


def _running_run(run_id: str) -> dict:
    """A run mid-flight: one step done, one still queued."""
    state = {
        "runId": run_id,
        "scenarioId": "s",
        "scenario": SCENARIO,
        "status": "running",
        "queue": ["b"],
        "ran": ["a"],
        "outputs": {},
        "startedAt": 1,
        "events": [],
    }
    save_run(state)
    return state


def _finish_events(run_id: str) -> list[dict]:
    return [e for e in load_events(run_id) if e.get("type") == "RunFinished"]


def test_cancelling_a_run_leaves_a_receipt():
    _running_run("r-cancel-receipt")

    state = cancel_run("r-cancel-receipt")

    receipt = state.get("receipt")
    assert isinstance(receipt, dict), "a cancelled run is a finished run"
    assert receipt["state"] == "cancelled"
    assert receipt["nodesRan"] == 1, "the step that did run is still on the record"
    assert receipt["evidence"] is False
    assert receipt["verified"] is False, "stopping a run proves nothing about it"
    assert receipt["finishedAt"] > 0


def test_cancelling_a_run_reports_it_as_cancelled_not_failed():
    """The event and the record must tell the same story."""
    _running_run("r-cancel-event")

    state = cancel_run("r-cancel-event")

    events = _finish_events("r-cancel-event")
    assert events, "cancelling ends the run, so it emits RunFinished"
    assert events[-1]["payload"]["state"] == "cancelled"
    assert events[-1]["payload"]["state"] == state["status"]


def test_a_dead_runs_receipt_says_the_owner_died():
    state = _running_run("r-dead-receipt")
    # NOT load_run(): reading a run whose owner is gone makes the store's
    # orphan reaper finish it first, so the assertions below would pass
    # against the in-store receipt without fail_dead_run doing anything.
    state = fail_dead_run(dict(state))

    receipt = state.get("receipt")
    assert isinstance(receipt, dict), "an abandoned run is a finished run"
    assert receipt["state"] == "failed"
    assert receipt["verified"] is False
    assert receipt["nodesRan"] == 1
    assert "exited" in receipt["meaning"], "say why, not just that it failed"


def test_a_dead_runs_event_agrees_with_its_status():
    state = _running_run("r-dead-event")

    state = fail_dead_run(dict(state))

    events = _finish_events("r-dead-event")
    assert events
    assert events[-1]["payload"]["state"] == state["status"] == "failed"


@pytest.mark.parametrize("nodes_ran", [0, 3])
def test_the_receipt_counts_the_steps_that_actually_ran(nodes_ran: int):
    """`nodesRan` is read off the record, not assumed."""
    run_id = f"r-count-{nodes_ran}"
    state = _running_run(run_id)
    state["ran"] = [f"n{i}" for i in range(nodes_ran)]
    save_run(state)

    state = cancel_run(run_id)

    assert state["receipt"]["nodesRan"] == nodes_ran
