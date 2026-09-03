"""The finish record every terminal path stamps on a run.

Borrowed from Limen's job record, which stores ``state = done`` and says
plainly that done means the process ended cleanly -- not that the work is
correct or the branch is safe to merge. Its coordinator still reads the
record, the diff, and the checks before merging.

wfgraph used to stop at ``status = "succeeded"``, which is the process outcome
wearing the costume of a verdict. A graph whose agent returned ok with an empty
summary finished every node and reported green; a cron operator reading the
stored run had no finish time and nothing to check the claim against.

The receipt keeps those ideas apart on the record itself:

``state``
    The process outcome -- the same fact ``status`` carries, in the same
    words. A run that ends four different ways should not describe itself in
    four different vocabularies.
``verified``
    Always ``False``. Nothing in this engine judges whether the work is right;
    something downstream has to.
``evidence``
    Did any node actually produce output, or did every one of them just return
    without saying anything. Computed the same way on every path -- a
    cancelled run that got real work done before it was stopped says so.
``meaning``
    The caveat spelled out in the record, for whoever reads it without docs.

There is one builder because there were three, and they disagreed: the walk
path reported ``state = "done"`` where the record said ``"succeeded"``, and
the abandoned/cancelled paths hardcoded ``evidence = False`` regardless of
what their nodes had produced.
"""

from __future__ import annotations

import time

# A run's own status words. The receipt speaks these, not a private dialect.
DONE = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

FINISHED_CLEANLY = (
    "The run ended cleanly. This does NOT mean the work is correct or "
    "complete -- read the outputs and checks before acting on it."
)
FINISHED_FAILED = (
    "The run ended with a failure. Read the errors on the record before "
    "retrying or acting on any partial output."
)
OWNER_DIED = (
    "The process driving this run exited before it finished. Any step that "
    "had already completed kept its result; nothing judged the work."
)
CANCELLED_BY_REQUEST = (
    "Someone stopped this run before it finished. Steps that had already run "
    "kept their results; the rest never ran."
)


def has_evidence(state: dict) -> bool:
    """Did any node on this run actually produce something?

    A node can finish without saying anything -- an agent that returns ok with
    an empty summary runs, completes, and leaves nothing behind. That is the
    shape of a false green, so the receipt records it as a fact rather than
    letting a clean exit imply work happened.
    """
    summaries = state.get("summaries") or {}
    if any(str(value or "").strip() for value in summaries.values()):
        return True

    # `outputs` values are structured ({"text": ...}), so stringifying the
    # container is always truthy -- look at the payload, not the wrapper.
    for value in (state.get("outputs") or {}).values():
        if isinstance(value, dict):
            if any(str(inner or "").strip() for inner in value.values()):
                return True
        elif str(value or "").strip():
            return True
    return False


def build_receipt(state: dict, *, outcome: str, meaning: str) -> dict:
    """Build the finish record for a run that has reached ``outcome``.

    Every terminal path goes through here: the graph finishing its own walk,
    the owner process dying, an operator cancelling, and the store reaping a
    run whose owner is gone.
    """
    finished_at = int(time.time() * 1000)
    started_at = int(state.get("startedAt") or finished_at)
    return {
        "state": outcome,
        "finishedAt": finished_at,
        "durationMs": max(0, finished_at - started_at),
        "nodesRan": len(state.get("ran") or []),
        "evidence": has_evidence(state),
        "verified": False,
        "meaning": meaning,
    }


def attach_receipt(state: dict, *, outcome: str, meaning: str) -> dict:
    """Stamp the receipt onto ``state`` and hand the receipt back."""
    receipt = build_receipt(state, outcome=outcome, meaning=meaning)
    state["receipt"] = receipt
    return receipt
