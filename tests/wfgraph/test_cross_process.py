"""Two processes, one workflow.

The engine keeps runs on disk but tracked liveness in an in-memory dict, so any
second process — a cron tick, a webhook, a CLI call — read "no live thread" for
a perfectly healthy run, reaped it, and started a duplicate. These tests hold
the boundary from the outside: they never consult the thread registry, only
what a foreign process can actually see on disk.
"""

import os

from wfgraph import lease
from wfgraph.runner import start_run
from wfgraph.store import list_runs, load_run, save_documents, save_run


def _agent(_goal, context, payload, _config):
    return {"ok": True, "summary": "did it", "verdict": "PASS", "output": {}}


def _scenario(*steps, edges=None):
    return {"steps": list(steps), "edges": list(edges or [])}


def _doc(workflow_id="wf", node_id="work"):
    return {
        "id": workflow_id,
        "name": workflow_id,
        "scenario": _scenario(
            {"id": node_id, "kind": "agent", "config": {"title": "Work", "goal": "go"}},
        ),
    }


def _put(monkeypatch, tmp_path, doc):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    save_documents([doc], doc["id"])


def _running_row(workflow_id, run_id, owner, node_id="work"):
    row = {
        "runId": run_id,
        "workflowId": workflow_id,
        "name": workflow_id,
        "scenario": _doc(workflow_id, node_id)["scenario"],
        "payload": None,
        "source": "manual",
        "status": "running",
        "queue": [node_id],
        "ran": [],
        "satisfied": [],
        "verdicts": {},
        "outputs": {},
        "summaries": {},
        "take": {},
        "loops": 0,
        "park": None,
        "wakeAt": None,
        "waitingEvent": None,
        "pauseRequested": False,
        "seq": 0,
        "startedAt": 1,
        "failed": False,
    }
    if owner is not None:
        row["owner"] = owner
    return row


def test_a_run_owned_by_a_live_foreign_process_is_not_reaped(tmp_path, monkeypatch):
    """THE bug: process B must not kill process A's healthy run.

    The owner is a live pid that is NOT in this interpreter's thread registry —
    exactly what a cron tick sees when the gateway owns the run. The correct
    behaviour is to adopt the existing run, not to mint a second one.
    """
    _put(monkeypatch, tmp_path, _doc())
    save_run(_running_row("wf", "owned-by-A", lease.stamp()))

    returned = start_run("wf", payload=None, source="cron", execute_fn=_agent, background=False)

    assert returned["runId"] == "owned-by-A", "a foreign live run was not adopted"
    rows = [r for r in list_runs("wf")]
    assert len(rows) == 1, f"a duplicate run was minted: {[r['runId'] for r in rows]}"
    assert load_run("owned-by-A")["status"] == "running", "a live run was reaped"


def test_a_run_whose_owner_died_is_still_reaped(tmp_path, monkeypatch):
    """The other half: a killed gateway must not wedge the workflow forever."""
    _put(monkeypatch, tmp_path, _doc())
    save_run(_running_row("wf", "zombie", {"pid": 999999, "startedAt": 1.0}))

    state = start_run("wf", payload=None, source="manual", execute_fn=_agent, background=False)

    assert state["runId"] != "zombie"
    assert load_run("zombie")["status"] == "failed"
    assert state["status"] == "succeeded"


def test_a_new_run_records_its_owner(tmp_path, monkeypatch):
    _put(monkeypatch, tmp_path, _doc())
    state = start_run("wf", payload=None, source="manual", execute_fn=_agent, background=False)

    owner = load_run(state["runId"])["owner"]
    assert owner["pid"] == os.getpid()
    assert owner["startedAt"] > 0


def test_a_legacy_run_without_an_owner_is_still_reapable(tmp_path, monkeypatch):
    """Runs written before the lease existed carry no owner block. With no live
    thread for them in this process they are dead, and must be replaced rather
    than blocking the workflow forever."""
    _put(monkeypatch, tmp_path, _doc())
    save_run(_running_row("wf", "legacy", owner=None))

    state = start_run("wf", payload=None, source="manual", execute_fn=_agent, background=False)

    assert state["runId"] != "legacy"
    assert load_run("legacy")["status"] == "failed"
