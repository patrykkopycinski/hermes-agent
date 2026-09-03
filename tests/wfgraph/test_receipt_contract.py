"""One receipt builder, one vocabulary, one definition of evidence.

There used to be three builders -- the walk in `runner`, the abandoned-run path
in `runtime`, and the reaper in `store`. They disagreed in two ways that a
reader of a stored run could not see:

* the walk stamped ``state = "done"`` where the record said ``"succeeded"``,
  so the run described its own outcome in two vocabularies at once
* the terminal paths hardcoded ``evidence = False``, claiming a cancelled run
  produced nothing even when its completed steps had produced real output

Both are the kind of drift that only shows up when someone builds a dashboard
on the receipt and finds a state word that never appears in `status`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wfgraph import receipt as receipt_states
from wfgraph.runner import cancel_run, start_run
from wfgraph.runtime import fail_dead_run
from wfgraph.store import load_run, save_documents, save_run

pytestmark = pytest.mark.usefixtures("wf_home")


def _finished_run(**overrides):
    """A stored run that has already produced real output."""
    state = {
        "runId": overrides.pop("run_id", "r-vocab"),
        "status": "running",
        "startedAt": 1,
        "ran": ["a"],
        "summaries": {"a": "wrote the file"},
        "outputs": {},
        "owner": {"pid": 1, "boot": "nope", "host": "nowhere"},
        "queue": [],
    }
    state.update(overrides)
    save_run(state)
    return state


# --- one vocabulary -------------------------------------------------------


def test_every_terminal_path_states_the_same_word_as_the_record():
    """`receipt.state` must be the word `status` uses, on every path.

    A dashboard grouping runs by `receipt.state` should never meet a value
    that `status` cannot produce.
    """
    state = _finished_run(run_id="r-vocab-cancel")
    cancelled = cancel_run("r-vocab-cancel")
    assert cancelled["receipt"]["state"] == cancelled["status"] == "cancelled"

    state = _finished_run(run_id="r-vocab-dead")
    dead = fail_dead_run(dict(state))
    assert dead["receipt"]["state"] == dead["status"] == "failed"


def test_a_finished_walk_states_the_same_word_as_the_record():
    """The normal path agrees too -- it used to say "done" where the record
    said "succeeded"."""
    save_documents(
        [
            {
                "id": "vocabwf",
                "name": "vocabwf",
                "scenario": {
                    "steps": [
                        {"id": "t", "kind": "trigger", "name": "start"},
                        {
                            "id": "a",
                            "kind": "agent",
                            "name": "step a",
                            "config": {"goal": "do it"},
                        },
                    ],
                    "edges": [{"source": "t", "target": "a"}],
                },
            }
        ]
    )

    out = start_run(
        "vocabwf",
        payload={},
        background=False,
        execute_fn=lambda *a, **k: {"ok": True, "summary": "did the work"},
    )
    run = load_run(out["runId"])

    assert run["receipt"]["state"] == run["status"] == "succeeded"


# --- one definition of evidence ------------------------------------------


def test_a_cancelled_run_that_did_real_work_says_so():
    """Stopping a run does not erase what its finished steps produced.

    The terminal paths used to hardcode ``evidence = False``, so a run
    cancelled after twenty successful steps reported the same emptiness as
    one cancelled before it started.
    """
    _finished_run(run_id="r-ev-cancel", summaries={"a": "wrote the file"})

    receipt = cancel_run("r-ev-cancel")["receipt"]

    assert receipt["evidence"] is True, "completed steps produced output"
    assert receipt["verified"] is False, "nothing judged that output"


def test_a_cancelled_run_that_did_nothing_says_that_too():
    _finished_run(run_id="r-ev-empty", ran=[], summaries={}, outputs={})

    receipt = cancel_run("r-ev-empty")["receipt"]

    assert receipt["evidence"] is False


def test_an_abandoned_run_reports_the_evidence_it_left_behind():
    state = _finished_run(run_id="r-ev-dead", summaries={"a": "half a result"})

    receipt = fail_dead_run(dict(state))["receipt"]

    assert receipt["evidence"] is True


def test_evidence_looks_inside_structured_outputs():
    """`outputs` values are dicts; stringifying the wrapper is always truthy."""
    empty = {"runId": "x", "summaries": {}, "outputs": {"a": {"text": "   "}}}
    real = {"runId": "x", "summaries": {}, "outputs": {"a": {"text": "result"}}}

    assert receipt_states.has_evidence(empty) is False
    assert receipt_states.has_evidence(real) is True


# --- the reaper agrees with everyone else --------------------------------


def test_the_reaper_stamps_the_same_shaped_receipt():
    """A run reaped by the store gets the same fields as one that walked."""
    state = _finished_run(run_id="r-reap", summaries={"a": "did a thing"})
    # An owner that cannot be alive: the reaper fires on the next read.
    state["owner"] = {"pid": 999999, "boot": "gone", "host": "elsewhere"}
    save_run(state)

    reaped = load_run("r-reap")

    receipt = reaped["receipt"]
    assert receipt["state"] == "failed"
    assert receipt["state"] == reaped["status"]
    assert receipt["evidence"] is True, "it had produced output before it died"
    assert receipt["verified"] is False
    assert set(receipt) == {
        "state",
        "finishedAt",
        "durationMs",
        "nodesRan",
        "evidence",
        "verified",
        "meaning",
    }
