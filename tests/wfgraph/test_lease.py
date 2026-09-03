"""The lease is what makes a run's owner knowable from another process."""
from __future__ import annotations

import os

import pytest

from wfgraph import lease


def test_a_run_owned_by_this_process_is_alive():
    state = {"runId": "r1", "owner": lease.stamp()}
    assert lease.owner_alive(state) is True


def test_a_run_owned_by_a_dead_pid_is_not_alive():
    state = {"runId": "r1", "owner": {"pid": 999999, "startedAt": 1.0}}
    assert lease.owner_alive(state) is False


def test_pid_reuse_does_not_resurrect_a_dead_owner():
    """Same pid, different process. The start-time fingerprint is what tells
    a recycled pid apart from the original owner."""
    owner = lease.stamp()
    owner["startedAt"] = float(owner["startedAt"]) - 5000.0
    assert lease.owner_alive({"runId": "r1", "owner": owner}) is False


def test_a_stamp_records_this_pid_and_its_start_time():
    owner = lease.stamp()
    assert owner["pid"] == os.getpid()
    assert isinstance(owner["startedAt"], float)
    assert owner["startedAt"] > 0


def test_a_run_with_no_owner_falls_back_to_the_thread_registry():
    """Runs written before the lease existed have no owner block. They must
    keep the old in-process meaning rather than being read as dead."""
    calls = []

    def fake_thread_alive(run_id):
        calls.append(run_id)
        return True

    assert lease.owner_alive({"runId": "legacy"}, thread_alive=fake_thread_alive) is True
    assert calls == ["legacy"]


def test_a_malformed_owner_block_counts_as_dead():
    """A corrupt marker must not wedge a workflow forever."""
    for bad in ({"pid": "nonsense"}, {"pid": None}, {"pid": -1}, "not-a-dict"):
        assert lease.owner_alive({"runId": "r", "owner": bad}) is False


def test_liveness_delegates_to_the_shared_no_kill_probe(monkeypatch):
    """os.kill(pid, 0) is NOT a no-op on Windows — it Ctrl+C's the target's
    whole console process group (bpo-14484). The engine must not hand-roll a
    probe; it must go through gateway.status._pid_exists, which picks a safe
    implementation per platform. Asserted by source, not by patching os.kill:
    psutil's own POSIX backend legitimately uses os.kill(pid, 0).
    """
    import inspect

    body = "".join(
        line for line in inspect.getsource(lease).splitlines(keepends=True)
        if not line.lstrip().startswith("#")
    )
    code = body.split('"""')[0] + "".join(body.split('"""')[2::2])
    assert "os.kill" not in code
    assert "_pid_exists" in code

    seen = []
    monkeypatch.setattr(lease, "_pid_exists", lambda pid: seen.append(pid) or True)
    lease.owner_alive({"runId": "r", "owner": {"pid": 4321, "startedAt": 0.0}})
    assert seen == [4321]
