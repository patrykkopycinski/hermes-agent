"""Reject a scenario the engine cannot honestly run.

These checks run once, before a run exists. They read the scenario as authored
-- steps, edges, kinds, gate arms, wait specs -- and raise if it describes
something the walk could not carry out: a wait nobody can parse, a gate whose
arms go nowhere, a back edge that would deadlock.

They live apart from the runner because they touch no run state. Nothing here
loads, saves, or advances anything; a validator that starts needing the store
is a sign the check belongs in the walk instead.
"""

from __future__ import annotations

from wfgraph.topology import config_of, kind_of, parse_wait_seconds, succs


class WorkflowGraphError(ValueError):
    """The graph itself is malformed — an unroutable gate, a dangling arm.

    A ValueError subclass on purpose: start_run's callers already treat
    ValueError as "bad workflow, tell the user", so this reaches them as a
    message rather than a traceback.
    """

SUPPORTED_KINDS = ("trigger", "agent", "gate", "wait", "human")


def reject_malformed_structure(scenario: dict, steps: list[dict]) -> None:
    """Structural checks that must pass before a run is worth starting.

    Each of these previously produced a run reporting "succeeded": an empty
    graph and an id-less step executed nothing, duplicate ids executed twice
    under one name and clobbered each other's per-node state, and an edge to a
    nonexistent node silently dropped a branch. Success is the worst available
    answer for all four - anything watching run status reads it as healthy.

    Reads scenario["steps"] raw rather than the filtered list: steps_of drops
    anything without an id (topology.py), which is exactly one of the cases
    that has to be caught rather than tolerated.
    """
    raw_steps = scenario.get("steps") or []

    for index, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            raise WorkflowGraphError(
                f"step at position {index} is a {type(step).__name__}, not an object"
            )
        step_id = step.get("id")
        if not step_id or not str(step_id).strip():
            raise WorkflowGraphError(
                f"step at position {index} has no 'id'; every step needs one "
                "because the engine keys sessions, retries and summaries by it"
            )

    if not steps:
        raise WorkflowGraphError(
            "workflow has no steps; there is nothing to run"
        )

    seen: set[str] = set()
    for step in steps:
        step_id = str(step["id"])
        if step_id in seen:
            raise WorkflowGraphError(
                f"duplicate step id {step_id!r}; ids must be unique because "
                "per-node state (sessions, tries, summaries) is keyed by them"
            )
        seen.add(step_id)

    for edge in scenario.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        for end in ("source", "target"):
            node = edge.get(end)
            if node is not None and str(node) not in seen:
                raise WorkflowGraphError(
                    f"edge {edge.get('id') or ''!r} names {end} {str(node)!r}, "
                    "which is not a step in this workflow"
                )


def reject_unparseable_waits(steps: list[dict]) -> None:
    """A timer whose spec cannot be parsed silently became a zero-length wait.

    park_wait falls back to seconds = 0 for anything parse_wait_seconds cannot
    read (waits.py), so "1 hour", "60" and "abc" all turned a deliberate pause
    into a no-op and the run sailed straight through. A soak period or a deploy
    gate written that way looks like it held and never did.

    An empty spec, or no 'until' block at all, stays a legal no-op: nothing was
    written, so nothing was misread.
    """
    for step in steps:
        if kind_of(step) != "wait":
            continue
        until = config_of(step).get("until") or {}
        if not isinstance(until, dict):
            raise WorkflowGraphError(
                f"step {step.get('id')!r}: 'until' is a {type(until).__name__}, "
                "expected an object like {'type': 'timer', 'spec': '1h'}"
            )
        # park_wait only ever reads 'type' and 'spec'. An until block written
        # with any other key ({'kind': 'event'}, {'until': '1h'}) defaults to a
        # timer with an empty spec, parses as zero, and the pause is skipped
        # while the run still reports success. A half-written block is just as
        # bad: {'kind': 'timer', 'spec': '1h'} has no readable type and
        # {'type': 'event', 'event': 'x'} parks on an empty event name. Refuse
        # anything we cannot read rather than pretend the wait happened.
        if until:
            unread = sorted(k for k in until if k not in {"type", "spec"})
            if unread or "type" not in until:
                shown = ", ".join(repr(k) for k in sorted(until))
                raise WorkflowGraphError(
                    f"step {step.get('id')!r}: 'until' is written with keys "
                    f"{shown}, but only 'type' and 'spec' are read, so it would "
                    "not wait at all. Write it as {'type': 'timer', 'spec': "
                    "'1h'} or {'type': 'event', 'spec': 'my.event'}."
                )
        if str(until.get("type") or "timer") != "timer":
            continue
        spec = str(until.get("spec") or "").strip()
        if not spec:
            continue
        if parse_wait_seconds(spec) is None:
            raise WorkflowGraphError(
                f"step {step.get('id')!r}: cannot read wait duration {spec!r}, "
                "so it would not wait at all. Use a number and a unit, like "
                "'30s', '5m', '1h' or '2d'."
            )


def reject_bad_gate_arms(steps: list[dict]) -> None:
    """A gate arm's 'when' must be an object; a bare string crashed mid-run."""
    for step in steps:
        if kind_of(step) != "gate":
            continue
        for arm in config_of(step).get("arms") or []:
            if not isinstance(arm, dict):
                continue
            when = arm.get("when")
            if when is not None and not isinstance(when, dict):
                raise WorkflowGraphError(
                    f"step {step.get('id')!r}: gate arm {arm.get('id')!r} has a "
                    f"{type(when).__name__} 'when' ({when!r}); expected an object "
                    "like {'mode': 'all-pass'} or {'mode': 'prose', 'source': ...}"
                )


def reject_deadlocked_back_edges(scenario: dict, steps: list[dict]) -> None:
    """A back-edge that is not marked as a loop is an unsatisfiable dependency.

    Draw gate -> check to express rework and forget ``"loop": True`` and check
    gains a predecessor that only ever runs after check itself. The run then
    sits until the readiness sweep gives up with 'never became ready', which
    names the symptom and hides the cause. Cheap to catch here, ugly at 3am.
    """
    ids = [s["id"] for s in steps]
    order = {node_id: i for i, node_id in enumerate(ids)}
    reachable: dict[str, set[str]] = {node_id: set() for node_id in ids}

    # Forward reachability over non-loop edges only.
    for _ in range(len(ids)):
        changed = False
        for edge in scenario.get("edges") or []:
            if edge.get("loop"):
                continue
            src, dst = edge.get("source"), edge.get("target")
            if src not in reachable or dst not in reachable:
                continue
            new = {dst} | reachable[dst]
            if not new <= reachable[src]:
                reachable[src] |= new
                changed = True
        if not changed:
            break

    for edge in scenario.get("edges") or []:
        if edge.get("loop"):
            continue
        src, dst = edge.get("source"), edge.get("target")
        if src not in order or dst not in order:
            continue
        if src in reachable.get(dst, set()):
            raise WorkflowGraphError(
                f"edge {src!r} -> {dst!r} closes a cycle but is not marked as a "
                f"loop, so {dst!r} waits on a step that only runs after it and "
                f"the run can never start it. Add \"loop\": true to that edge if "
                "it is a rework path."
            )


def reject_unrouted_gate_arms(scenario: dict, steps: list[dict]) -> None:
    """A gate arm whose id labels no outgoing edge is caught before the run.

    ``_run_gate`` already refuses to act on an arm it cannot route, with a
    clear message -- but it raises at the moment the gate is reached, which is
    on the far side of every upstream agent step. On the real path those are
    model calls: the graph was unroutable when it was written, and the author
    pays for a full upstream run to find out.

    The mismatch is nearly always a typo between the arm's id and the edge's
    ``sourceHandle`` (``pass`` vs ``passed``), which is exactly the kind of
    thing that is free to catch here and expensive to catch there.

    Arms with no id are left alone: an unlabelled arm cannot be matched
    against a handle at all, and ``_run_gate`` owns that diagnosis with a
    better message than this check could give.

    Resolution is delegated to ``succs`` -- the same function the runner
    routes with -- rather than re-derived here. An edge with no
    ``sourceHandle`` answers to the handle ``"out"``, and a rework arm is
    routinely drawn as a bare ``loop`` edge; a hand-rolled set of declared
    handles misses both and rejects graphs that run correctly today.
    """
    for step in steps:
        if kind_of(step) != "gate":
            continue
        node_id = str(step.get("id"))
        for arm in config_of(step).get("arms") or []:
            if not isinstance(arm, dict):
                continue
            arm_id = str(arm.get("id") or "").strip()
            if not arm_id:
                continue  # _run_gate's problem, and it says it better
            if succs(scenario, node_id, arm_id):
                continue
            labelled = sorted(
                {
                    str(edge.get("sourceHandle") or "out")
                    for edge in scenario.get("edges") or []
                    if isinstance(edge, dict) and str(edge.get("source")) == node_id
                }
            )
            known = ", ".join(repr(h) for h in labelled) or "none"
            raise WorkflowGraphError(
                f"gate {node_id!r}: arm {arm_id!r} has no outgoing edge, so the "
                f"gate could match it and have nowhere to go. Edges leaving "
                f"{node_id!r} answer to: {known}. Give the arm's edge a "
                f"sourceHandle of {arm_id!r}."
            )


def reject_misshapen_edges(scenario: dict, steps: list[dict]) -> None:
    """Reject edges whose keys are wrong (e.g. from/to) before a run starts.

    edges_of() drops any edge dict without source/target — by design for
    malformed junk, but an author writing {\"from\": a, \"to\": b} (a common
    guess) gets a graph where every node is a root and everything runs at
    once, including nodes meant to be gated behind a human approval.
    Caught live 2026-09-03: a mis-authored watch-response workflow ran its
    `act` step BEFORE the human park because its edges used from/to.
    """
    known_ids = {s["id"] for s in steps}
    for index, edge in enumerate(scenario.get("edges") or []):
        if not isinstance(edge, dict):
            raise WorkflowGraphError(
                f"edge at position {index} is a {type(edge).__name__}, not an object"
            )
        if not edge.get("source") or not edge.get("target"):
            hint = ""
            if edge.get("from") or edge.get("to"):
                hint = " It looks like from/to keys — this engine uses source/target."
            raise WorkflowGraphError(
                f"edge at position {index} ({edge}) is missing source/target"
                f" and would be silently ignored.{hint} Every node it was meant"
                " to sequence would run at once as a root."
            )
        if edge["source"] not in known_ids or edge["target"] not in known_ids:
            raise WorkflowGraphError(
                f"edge {edge['source']} -> {edge['target']} references a step"
                " that does not exist in this workflow."
            )


def reject_unknown_kinds(steps: list[dict]) -> None:
    """Fail a malformed graph at start, loudly, instead of running nothing."""
    unknown = [
        (str(step.get("id") or "?"), str(kind_of(step) or ""))
        for step in steps
        if kind_of(step) not in SUPPORTED_KINDS
    ]
    if not unknown:
        return
    listed = ", ".join(f"'{node_id}' (kind '{kind}')" for node_id, kind in unknown)
    raise WorkflowGraphError(
        f"Unknown step kind in this workflow: {listed}. The engine only runs "
        f"{', '.join(SUPPORTED_KINDS)}. A step of any other kind is skipped "
        "silently and its successors never run, so the workflow would report "
        "success having done nothing. Fix the kind, or remove the step."
    )
