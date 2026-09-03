"""Every way a run can end must leave a receipt.

An AST sweep over the package found three terminal status assignments with no
receipt in the enclosing function, and all three reproduce live:

  * `respond(..., "denied")` with onFail=halt -- a person rejects the work
  * `runtime.spawn`'s except-handler -- a node raises
  * a cancel *signal* absorbed mid-walk (as opposed to `cancel_run`)

Each ends the run with `receipt: None`: no finishedAt, no statement about
whether work landed. That is the false-green shape the receipt exists to close,
and my earlier audit missed all three because it only grepped the three modules
I happened to be editing.

These tests drive each path end to end and assert the receipt is there and says
the same word as the run record.
"""

import os
import tempfile
import time

import pytest

from wfgraph.runner import cancel_run, respond, start_run
from wfgraph.runtime import signal
from wfgraph.store import load_run, save_documents

pytestmark = pytest.mark.usefixtures("wf_home")

TERMINAL = {"succeeded", "failed", "cancelled"}


def _passing(goal, context, payload, cfg):
    return {"ok": True, "text": "done", "verdict": "PASS"}


def _assert_receipt_agrees(state):
    """A finished run states its outcome once, in both places."""
    receipt = state.get("receipt")
    assert isinstance(receipt, dict), f"no receipt on a {state.get('status')} run"
    assert receipt["state"] == state["status"], (
        f"receipt says {receipt['state']!r}, record says {state['status']!r}"
    )
    assert receipt.get("finishedAt"), "a finished run needs a finish time"
    assert "verified" in receipt, "a receipt must say whether work was verified"


# --------------------------------------------------------------------------
# a person rejects the work
# --------------------------------------------------------------------------

HUMAN_DOC = {
    "id": "humwf",
    "name": "humwf",
    "scenario": {
        "steps": [
            {"id": "t", "kind": "trigger", "title": "t"},
            {
                "id": "ask",
                "kind": "human",
                "title": "ask",
                "config": {"assignee": "pat", "goal": "ship it?", "onFail": "halt"},
            },
            {"id": "after", "kind": "agent", "title": "after"},
        ],
        "edges": [
            {"source": "t", "target": "ask"},
            {"source": "ask", "target": "after"},
        ],
    },
}


def test_a_human_denial_finishes_with_a_receipt():
    save_documents([HUMAN_DOC], current_id="humwf")
    st = start_run("humwf", payload={}, background=False, execute_fn=_passing)
    assert st["status"] == "waiting_human"

    final = respond(st["runId"], "ask", "denied", by="pat", execute_fn=_passing)

    assert final["status"] == "failed"
    _assert_receipt_agrees(final)
    assert final["receipt"]["verified"] is False, (
        "a denied run proves nothing about the work"
    )


def test_a_human_denial_receipt_survives_a_reload():
    """The reader sees the record on disk, not the in-memory dict."""
    save_documents([HUMAN_DOC], current_id="humwf")
    st = start_run("humwf", payload={}, background=False, execute_fn=_passing)
    respond(st["runId"], "ask", "denied", by="pat", execute_fn=_passing)

    disk = load_run(st["runId"])
    _assert_receipt_agrees(disk)


def test_an_approved_human_step_still_runs_on(): 
    """Guard rail: the fix must not finish a run the person approved."""
    save_documents([HUMAN_DOC], current_id="humwf")
    st = start_run("humwf", payload={}, background=False, execute_fn=_passing)

    final = respond(st["runId"], "ask", "approved", by="pat", execute_fn=_passing)

    assert final["status"] == "succeeded"
    assert "after" in final["ran"], "the step after the approval must still run"


# --------------------------------------------------------------------------
# a node raises
# --------------------------------------------------------------------------

BOOM_DOC = {
    "id": "boomwf",
    "name": "boomwf",
    "scenario": {
        "steps": [
            {"id": "t", "kind": "trigger", "title": "t"},
            {"id": "work", "kind": "agent", "title": "work"},
        ],
        "edges": [{"source": "t", "target": "work"}],
    },
}


def test_a_crashing_run_finishes_with_a_receipt():
    save_documents([BOOM_DOC], current_id="boomwf")

    def explode(goal, context, payload, cfg):
        raise RuntimeError("the tool blew up")

    st = start_run("boomwf", payload={}, background=True, execute_fn=explode)

    deadline = time.time() + 10
    cur = load_run(st["runId"])
    while time.time() < deadline:
        cur = load_run(st["runId"])
        if cur and cur.get("status") in TERMINAL:
            break
        time.sleep(0.05)

    assert cur["status"] == "failed"
    _assert_receipt_agrees(cur)
    assert cur["receipt"]["verified"] is False


# --------------------------------------------------------------------------
# a cancel signal absorbed mid-walk
# --------------------------------------------------------------------------

CHAIN_DOC = {
    "id": "sigwf",
    "name": "sigwf",
    "scenario": {
        "steps": [
            {"id": "t", "kind": "trigger", "title": "t"},
            {"id": "a", "kind": "agent", "title": "a"},
            {"id": "b", "kind": "agent", "title": "b"},
            {"id": "c", "kind": "agent", "title": "c"},
        ],
        "edges": [
            {"source": "t", "target": "a"},
            {"source": "a", "target": "b"},
            {"source": "b", "target": "c"},
        ],
    },
}


def test_a_cancel_signal_finishes_with_a_receipt():
    """`cancel_run` is not the only way a run gets cancelled: a signal raised
    while the walk is in flight is absorbed by the loop itself."""
    save_documents([CHAIN_DOC], current_id="sigwf")

    holder = {}

    def cancel_midway(goal, context, payload, cfg):
        if holder.get("runId") and not holder.get("fired"):
            holder["fired"] = True
            signal(holder["runId"], "cancel")
        return {"ok": True, "text": "done", "verdict": "PASS"}

    import wfgraph.runner as runner_mod

    original = runner_mod.save_run

    def capture(state):
        holder.setdefault("runId", state.get("runId"))
        return original(state)

    runner_mod.save_run = capture
    try:
        st = start_run("sigwf", payload={}, background=False, execute_fn=cancel_midway)
    finally:
        runner_mod.save_run = original

    disk = load_run(st["runId"])
    assert disk["status"] == "cancelled"
    _assert_receipt_agrees(disk)
    assert disk["receipt"]["verified"] is False


def test_an_explicit_cancel_still_carries_its_receipt():
    """Guard rail: the path that already worked must keep working.

    Park the run on a human step so it is genuinely mid-flight and cannot
    finish before the cancel lands -- a race here would skip the assertion.
    """
    save_documents([HUMAN_DOC], current_id="humwf")

    st = start_run("humwf", payload={}, background=False, execute_fn=_passing)
    assert st["status"] == "waiting_human", "run must still be open to cancel it"

    cancel_run(st["runId"])
    disk = load_run(st["runId"])
    assert disk["status"] == "cancelled"
    _assert_receipt_agrees(disk)
