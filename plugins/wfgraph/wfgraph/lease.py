"""Who owns a run, answerable from any process.

``runtime.thread_alive`` can only see threads in the interpreter that asked.
That is fine for one long-lived ``hermes serve``, and wrong the moment a cron
tick, a webhook, or a second CLI invocation looks at the same run directory:
every foreign process reads "no live thread" for a perfectly healthy run and
helpfully reaps it, so the workflow runs twice and one copy is marked failed
while it is still writing.

The fix is to record *who* is running it in the run file itself. A pid alone is
not enough — pids get recycled, and a recycled pid would resurrect a dead
owner — so the marker is a pid plus that process's start time, which together
identify one specific process.

Liveness routes through the gateway's ``_pid_exists``. On Windows
``os.kill(pid, 0)`` is not a no-op: it delivers a Ctrl+C to the target's whole
console process group (bpo-14484). Probing a run's owner must never kill it.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from gateway.status import _pid_exists
from hermes_cli.active_sessions import _process_start_time

# A process that started within this many seconds of the recorded time is the
# same process. Start times come from different clocks across platforms and are
# rounded differently by the OS; an exact float match is not portable.
_START_TIME_TOLERANCE_S = 2.0


def stamp(pid: Optional[int] = None) -> dict:
    """Build the owner marker for a process (this one unless told otherwise)."""
    pid = os.getpid() if pid is None else int(pid)
    started = _process_start_time(pid)
    return {"pid": pid, "startedAt": float(started) if started else 0.0}


def owner_alive(
    state: dict,
    thread_alive: Optional[Callable[[str], bool]] = None,
) -> bool:
    """True when the process that owns this run is still running.

    Runs written before the lease existed carry no ``owner`` block. Those fall
    back to the in-process thread registry so nothing regresses for a single
    long-lived server.
    """
    owner: Any = state.get("owner")

    if not isinstance(owner, dict):
        if owner is None and thread_alive is not None:
            return bool(thread_alive(state.get("runId", "")))
        if owner is None:
            from wfgraph.runtime import thread_alive as _registry_alive

            return bool(_registry_alive(state.get("runId", "")))
        return False  # a corrupt marker must not wedge the workflow forever

    pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False

    if not _pid_exists(pid):
        return False

    # The pid is live, but is it the same process that claimed the run?
    recorded = owner.get("startedAt")
    if not isinstance(recorded, (int, float)) or recorded <= 0:
        # Owner claimed without a usable start time. The pid is the only
        # evidence available, and it exists.
        return True

    actual = _process_start_time(pid)
    if not actual:
        return True  # cannot fingerprint on this platform; trust the pid

    return abs(float(actual) - float(recorded)) <= _START_TIME_TOLERANCE_S
