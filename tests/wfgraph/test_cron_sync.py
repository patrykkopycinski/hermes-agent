"""Cron sync must converge on exactly one job per workflow.

`sync_cron_jobs` reconciles cron triggers against real cron jobs, and had no
test at all -- the suite only covered webhook routes. Two behaviours matter and
one of them was broken:

  * a workflow that stops being cron-triggered loses its job  (worked)
  * a workflow never ends up with two jobs                    (did not)

The dedupe built `{workflow_id: job}` from the owned-job list, so two jobs
carrying the same workflow_id collapsed to one entry and the loser was never
removed. It ticks forever, firing the workflow a second time on every schedule,
and no later sync heals it -- each one keeps whichever job the dict saw last.

Duplicates are reachable: `sync_cron_jobs` reads the existing jobs, then creates,
with no lock in between. Two syncs interleaving (a cron tick racing a canvas
save) both see "no job yet" and both create one. The reentrancy test below is
that interleaving, made deterministic.

These tests inject a fake `cron.jobs` so they never touch real cron state.
"""

import sys
import types

import pytest

from wfgraph.triggers import sync_cron_jobs

pytestmark = pytest.mark.usefixtures("wf_home")


def _cron_doc(workflow_id="wf1", spec="0 9 * * *"):
    return {
        "id": workflow_id,
        "name": workflow_id,
        "scenario": {
            "steps": [
                {
                    "id": "t",
                    "kind": "trigger",
                    "title": "t",
                    "config": {"on": {"type": "cron", "spec": spec}},
                }
            ],
            "edges": [],
        },
    }


def _manual_doc(workflow_id="wf1"):
    return {
        "id": workflow_id,
        "name": workflow_id,
        "scenario": {
            "steps": [
                {
                    "id": "t",
                    "kind": "trigger",
                    "title": "t",
                    "config": {"on": {"type": "manual"}},
                }
            ],
            "edges": [],
        },
    }


class FakeCron:
    """Stands in for `cron.jobs`, recording what the sync asks for."""

    def __init__(self):
        self.jobs = {}
        self.calls = []
        self._n = 0

    def create_job(self, **kw):
        self._n += 1
        jid = f"job{self._n}"
        self.jobs[jid] = {
            "id": jid,
            "schedule_display": kw.get("schedule"),
            "origin": kw.get("origin") or {},
        }
        self.calls.append(("create", jid))
        return self.jobs[jid]

    def update_job(self, jid, updates):
        self.calls.append(("update", jid))
        if jid in self.jobs and "schedule" in updates:
            self.jobs[jid]["schedule_display"] = updates["schedule"]
        return self.jobs.get(jid)

    def remove_job(self, jid):
        self.calls.append(("remove", jid))
        self.jobs.pop(jid, None)

    def list_jobs(self, include_disabled=False):
        return list(self.jobs.values())

    def owned_for(self, workflow_id):
        return [
            j
            for j in self.jobs.values()
            if (j.get("origin") or {}).get("workflow_id") == workflow_id
        ]


@pytest.fixture
def cron(monkeypatch):
    fake = FakeCron()
    module = types.ModuleType("cron.jobs")
    module.create_job = fake.create_job
    module.update_job = fake.update_job
    module.remove_job = fake.remove_job
    module.list_jobs = fake.list_jobs
    package = types.ModuleType("cron")
    package.jobs = module
    monkeypatch.setitem(sys.modules, "cron", package)
    monkeypatch.setitem(sys.modules, "cron.jobs", module)
    return fake


def test_a_cron_trigger_creates_one_job(cron):
    sync_cron_jobs([_cron_doc()])
    assert len(cron.owned_for("wf1")) == 1


def test_syncing_twice_does_not_create_a_second_job(cron):
    sync_cron_jobs([_cron_doc()])
    sync_cron_jobs([_cron_doc()])
    assert len(cron.owned_for("wf1")) == 1


def test_dropping_the_cron_trigger_removes_the_job(cron):
    sync_cron_jobs([_cron_doc()])
    sync_cron_jobs([_manual_doc()])
    assert cron.owned_for("wf1") == []


def test_a_changed_schedule_updates_the_existing_job(cron):
    sync_cron_jobs([_cron_doc(spec="0 9 * * *")])
    sync_cron_jobs([_cron_doc(spec="30 6 * * *")])

    owned = cron.owned_for("wf1")
    assert len(owned) == 1
    assert owned[0]["schedule_display"] == "30 6 * * *"


def test_duplicate_jobs_for_one_workflow_are_reconciled_to_one(cron):
    """A workflow must never keep two jobs: it would fire twice per schedule."""
    cron.jobs["dup1"] = {
        "id": "dup1",
        "schedule_display": "0 9 * * *",
        "origin": {"kind": "workflow", "workflow_id": "wf1"},
    }
    cron.jobs["dup2"] = {
        "id": "dup2",
        "schedule_display": "0 9 * * *",
        "origin": {"kind": "workflow", "workflow_id": "wf1"},
    }

    sync_cron_jobs([_cron_doc()])

    assert len(cron.owned_for("wf1")) == 1, "a duplicate job survived the sync"


def test_a_race_between_two_syncs_still_leaves_one_job(cron):
    """The interleaving that creates duplicates in the first place.

    `sync_cron_jobs` reads the job list, then creates -- with no lock between.
    Driving a second sync from inside the first reproduces two processes racing
    deterministically.
    """
    import wfgraph.triggers as triggers

    original = triggers._write_tick_script
    depth = {"n": 0}

    def reentrant(workflow_id):
        depth["n"] += 1
        if depth["n"] == 1:
            sync_cron_jobs([_cron_doc()])
        return original(workflow_id)

    # Swap by hand, not via monkeypatch: `monkeypatch.undo()` would also revert
    # the `cron` fixture's sys.modules entries (same monkeypatch instance), and
    # the healing sync below would then talk to the real cron instead of the
    # fake -- a green test proving nothing.
    triggers._write_tick_script = reentrant
    try:
        sync_cron_jobs([_cron_doc()])
    finally:
        triggers._write_tick_script = original

    assert len(cron.owned_for("wf1")) == 2, (
        "the race did not produce the duplicate this test exists to heal"
    )

    # The next sync must heal it.
    sync_cron_jobs([_cron_doc()])
    assert len(cron.owned_for("wf1")) == 1, (
        "duplicate cron jobs never heal: the workflow fires twice per schedule"
    )


def test_jobs_for_other_workflows_are_left_alone(cron):
    """Guard rail: reconciling wf1 must not touch another workflow's job."""
    sync_cron_jobs([_cron_doc("wf1"), _cron_doc("wf2")])
    assert len(cron.owned_for("wf1")) == 1
    assert len(cron.owned_for("wf2")) == 1

    sync_cron_jobs([_cron_doc("wf1"), _manual_doc("wf2")])
    assert len(cron.owned_for("wf1")) == 1, "wf1 lost its job while wf2 was dropped"
    assert cron.owned_for("wf2") == []


def test_duplicates_of_a_dropped_workflow_are_all_removed(cron):
    """Every job for a workflow that lost its trigger must go, not just one."""
    cron.jobs["dup1"] = {
        "id": "dup1",
        "schedule_display": "0 9 * * *",
        "origin": {"kind": "workflow", "workflow_id": "wf1"},
    }
    cron.jobs["dup2"] = {
        "id": "dup2",
        "schedule_display": "0 9 * * *",
        "origin": {"kind": "workflow", "workflow_id": "wf1"},
    }

    sync_cron_jobs([_manual_doc()])

    assert cron.owned_for("wf1") == [], "a job survived for a workflow with no trigger"


def test_a_users_own_cron_job_is_never_touched(cron):
    """Guard rail: only jobs this module created are ours to remove."""
    cron.jobs["mine"] = {
        "id": "mine",
        "schedule_display": "0 0 * * *",
        "origin": {"kind": "user"},
    }

    sync_cron_jobs([_manual_doc()])

    assert "mine" in cron.jobs, "the sync removed a job it does not own"
