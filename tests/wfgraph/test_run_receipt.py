"""A finished run must carry a receipt that separates outcome from verdict.

Ported from Limen's job record. Limen writes `state = done` for a job and is
explicit in its README that `done` means the process ended cleanly -- "Neither
state means the ticket is finished or the branch is safe to merge." The
coordinator still has to read the record, the diff, and the checks.

wfgraph had the opposite default: `_finish` set `status = "succeeded"` whenever
nothing raised, and the stored run carried no finish time and no evidence of
work. A cron or webhook operator reading that run sees "succeeded" and has
nothing to check it against -- a graph whose agent returned ok with an empty
summary is indistinguishable from one that did the job.

These tests pin the two halves of that split:
  - `status` stays the process outcome (unchanged, still "succeeded").
  - `receipt` carries when it ended, what actually ran, and whether there is
    any evidence the work happened -- so "succeeded with no evidence" is
    legible instead of green.
"""

from __future__ import annotations

import pytest

from wfgraph.runner import start_run
from wfgraph.store import load_run, save_documents

pytestmark = pytest.mark.usefixtures("wf_home")


def _wf(wid: str = "recwf") -> dict:
    return {
        "id": wid,
        "name": wid,
        "scenario": {
            "steps": [
                {"id": "t", "kind": "trigger", "name": "start"},
                {"id": "a", "kind": "agent", "name": "step a", "config": {"goal": "do it"}},
            ],
            "edges": [{"source": "t", "target": "a"}],
        },
    }


def test_receipt_records_when_the_run_ended():
    """startedAt alone cannot answer 'how long did this take' or 'is it stale'."""
    save_documents([_wf()])

    out = start_run(
        "recwf",
        payload={},
        background=False,
        execute_fn=lambda *a, **k: {"ok": True, "summary": "wrote the file"},
    )

    run = load_run(out["runId"])
    receipt = run["receipt"]

    assert receipt["finishedAt"] >= run["startedAt"]
    assert receipt["durationMs"] >= 0
    assert receipt["state"] == "done"


def test_done_does_not_claim_the_work_is_correct():
    """Limen's core lesson: a clean exit is not a verdict on the work.

    The receipt has to say so in the record itself, not in documentation a
    cron operator will never read.
    """
    save_documents([_wf()])

    out = start_run(
        "recwf",
        payload={},
        background=False,
        execute_fn=lambda *a, **k: {"ok": True, "summary": "wrote the file"},
    )

    receipt = load_run(out["runId"])["receipt"]

    assert receipt["state"] == "done"
    assert receipt["verified"] is False
    assert "not" in receipt["meaning"].lower()


def test_succeeded_with_no_evidence_is_flagged():
    """The false-green case: every node 'ran', nothing was produced.

    An agent that returns ok with an empty summary drives the graph to
    completion. Status is legitimately "succeeded" -- the graph did complete --
    but there is no evidence any work happened, and that has to be visible.
    """
    save_documents([_wf()])

    out = start_run(
        "recwf",
        payload={},
        background=False,
        execute_fn=lambda *a, **k: {"ok": True, "summary": "   "},
    )

    run = load_run(out["runId"])

    assert run["status"] == "succeeded"
    assert run["receipt"]["evidence"] is False
    assert run["receipt"]["nodesRan"] == 2


def test_real_output_counts_as_evidence():
    """The control for the test above -- a run that did produce something."""
    save_documents([_wf()])

    out = start_run(
        "recwf",
        payload={},
        background=False,
        execute_fn=lambda *a, **k: {"ok": True, "summary": "created HELLO.txt"},
    )

    receipt = load_run(out["runId"])["receipt"]

    assert receipt["evidence"] is True
    assert receipt["state"] == "done"


def test_failed_run_gets_a_receipt_too():
    """A failure needs the same finish stamp; otherwise duration is unknowable."""
    save_documents([_wf()])

    out = start_run(
        "recwf",
        payload={},
        background=False,
        execute_fn=lambda *a, **k: {"ok": False, "error": "model is required"},
    )

    run = load_run(out["runId"])
    receipt = run["receipt"]

    assert run["status"] == "failed"
    assert receipt["state"] == "failed"
    assert receipt["verified"] is False
    assert receipt["finishedAt"] >= run["startedAt"]
