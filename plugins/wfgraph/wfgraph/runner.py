"""Walk a scenario and emit the canvas event log.

Topology is real. Work is real when ``execute_fn`` calls a model; tests
inject a stub. Human and wait steps persist a park so closing the app
does not lose the run — resume via ``respond`` / ``resolve_event`` /
``tick_timers``.

This file is the loop and the doors into it. What it walks over, waits on and
stands in for lives beside it, so each can be read and tested without the loop:

    topology  reading the authored scenario — steps, wires, conditions
    runtime   the per-run plumbing every thread shares — lock, seq, signals
    waits     the steps that stop, and the clocks that start them again
    fake      the recording stand-in that calls no model
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from wfgraph import fake
from wfgraph.lease import owner_alive
from wfgraph.lease import stamp as lease_stamp
from wfgraph.runtime import (
    absorb_signals,
    clear_signal,
    emit,
    fail_dead_run,
    lock_for,
    signal,
    spawn,
)
from wfgraph.store import (
    active_run,
    get_document,
    list_runs,
    load_documents,
    load_events,
    load_run,
    new_run_id,
    save_run,
    upsert_document,
)
from wfgraph.topology import (
    between,
    by_id,
    config_of,
    holds,
    kind_of,
    parse_wait_seconds,
    preds,
    scenario_of,
    steps_of,
    succs,
    title_of,
)
from wfgraph.waits import (
    finish_wait,
    park_human,
    park_wait,
    rearm,
    tick_polls,
    tick_timers,
)

ExecuteFn = Callable[[str, str, Any, dict], dict]

_execute_fn: ExecuteFn | None = None


def set_execute_fn(fn: ExecuteFn | None) -> None:
    global _execute_fn
    _execute_fn = fn


def _context_for(state: dict, node_id: str) -> str:
    parts = []
    for pred in preds(state["scenario"], node_id, loops=True):
        summary = (state.get("summaries") or {}).get(pred)
        output = (state.get("outputs") or {}).get(pred)
        if summary:
            parts.append(f"{pred}: {summary}")
        if output:
            parts.append(f"{pred} output: {output}")
    return "\n".join(parts)


def _fresh_state(workflow_id: str, scenario: dict, payload: Any, source: str, name: str) -> dict:
    return {
        "runId": new_run_id(),
        "workflowId": workflow_id,
        "name": name,
        "scenario": scenario,
        "payload": payload,
        "source": source,
        "status": "running",
        "queue": [],
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
        "startedAt": int(time.time() * 1000),
        "failed": False,
        "tries": {},
        "inFlight": [],
        "sessions": {},
        # Who is driving this run, readable from any process. See wfgraph.lease.
        "owner": lease_stamp(),
    }


def start_run(
    workflow_id: str,
    *,
    scenario: dict | None = None,
    payload: Any = None,
    source: str = "manual",
    execute_fn: ExecuteFn | None = None,
    background: bool = True,
    fake: bool = False,
) -> dict:
    doc = get_document(workflow_id)
    if doc is not None:
        workflow_id = doc["id"]
    if scenario is None:
        if doc is None:
            raise ValueError(f"No workflow called '{workflow_id}'.")
        scenario = scenario_of(doc)
    else:
        if doc is not None:
            upsert_document({**doc, "scenario": scenario})
        else:
            upsert_document({"id": workflow_id, "name": workflow_id, "scenario": scenario})
            doc = get_document(workflow_id)
    name = (doc or {}).get("name") or workflow_id
    existing = active_run(workflow_id)
    if existing is not None:
        # Liveness must be judged by the run's recorded owner, not by this
        # interpreter's thread registry: a cron tick or webhook process has an
        # empty registry and would otherwise reap a perfectly healthy run and
        # start a duplicate alongside it.
        if existing.get("status") == "running" and not owner_alive(existing):
            fail_dead_run(existing)
        else:
            return existing

    state = _fresh_state(workflow_id, scenario, payload, source, name)
    if fake:
        state["fake"] = True
    steps = steps_of(scenario)
    _reject_malformed_structure(scenario, steps)
    _reject_unknown_kinds(steps)
    _reject_bad_gate_arms(steps)
    _reject_unparseable_waits(steps)
    _reject_deadlocked_back_edges(scenario, steps)
    entries = [s["id"] for s in steps if not preds(scenario, s["id"])]
    if not entries and steps:
        entries = [steps[0]["id"]]
    state["queue"] = list(entries)
    save_run(state)
    emit(state, "RunStarted", {"scenario": name})
    if background:
        spawn(state["runId"], execute_fn)
    else:
        # The inline path is the durable one (cron ticks, webhooks, tests).
        # spawn() already converts a crash into a failed run; without the same
        # guard here an exception escapes with the run file still at
        # "running", leaving a workflow that neither progresses nor finishes.
        try:
            advance(state["runId"], execute_fn=execute_fn)
        except Exception as exc:
            live = load_run(state["runId"]) or state
            if live.get("status") not in {"succeeded", "failed", "cancelled"}:
                live["status"] = "failed"
                live["failed"] = True
                live["pauseRequested"] = False
                save_run(live)
                emit(live, "RunFinished", {"state": "failed", "error": str(exc)})
            raise
    return load_run(state["runId"]) or state


def start_from_trigger(
    workflow_id: str,
    *,
    source: str = "cron",
    payload: Any = None,
    background: bool = False,
    execute_fn: ExecuteFn | None = None,
) -> dict:
    """Start a run from a cron tick or an inbound webhook.

    ``background`` defaults to False here, unlike ``start_run``. A trigger
    fires inside a short-lived script that cron or the webhook adapter runs
    and then reaps; spawning a daemon thread and returning would let the
    interpreter exit out from under the walk, leaving the run wedged at
    ``running`` with an empty ``ran`` list and no thread left to finish it.
    The caller's process IS the run's lifetime, so walk it inline and let the
    script's exit mean the run is genuinely over.
    """
    return start_run(
        workflow_id,
        payload=payload,
        source=source,
        background=background,
        execute_fn=execute_fn,
    )


def start_matching(
    *,
    event: str,
    payload: Any = None,
    source: str = "event",
    background: bool = True,
    execute_fn: ExecuteFn | None = None,
) -> list[dict]:
    """Start every workflow whose trigger listens for this event, and resume parks."""
    started = []
    for run_id in resolve_event(event, payload, background=background, execute_fn=execute_fn):
        parked = load_run(run_id)
        if parked is not None:
            started.append(parked)
    needle = (event or "").strip().lower()
    if not needle:
        return started
    for doc in load_documents()["docs"]:
        scenario = scenario_of(doc)
        for step in steps_of(scenario):
            if kind_of(step) != "trigger":
                continue
            on = config_of(step).get("on") or {}
            if on.get("type") != "event":
                continue
            if str(on.get("spec") or "").strip().lower() != needle:
                continue
            if active_run(doc["id"]) is not None:
                continue
            started.append(
                start_run(
                    doc["id"],
                    payload=payload,
                    source=source,
                    background=background,
                    execute_fn=execute_fn,
                )
            )
            break
    return started


def advance(run_id: str, *, execute_fn: ExecuteFn | None = None) -> dict:
    with lock_for(run_id):
        return _advance(run_id, execute_fn)


def _advance(run_id: str, execute_fn: ExecuteFn | None) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    if state.get("status") in {"succeeded", "failed", "cancelled"}:
        return state
    if state.get("status") == "paused":
        return state
    if state.get("park"):
        return state

    # Whoever is about to drive this run owns it from here. Without this a run
    # resumed in a new process would keep the previous process's marker and
    # read as dead to everyone, including itself.
    current = lease_stamp()
    if state.get("owner") != current:
        state["owner"] = current
        save_run(state)

    fn = execute_fn or _execute_fn
    scenario = state["scenario"]
    steps = by_id(scenario)

    leftover = [node_id for node_id in (state.get("inFlight") or []) if node_id not in state["ran"] and node_id not in state["queue"]]
    if leftover:
        state["queue"] = leftover + state["queue"]
        state["inFlight"] = []
        save_run(state)

    def park_pause() -> dict:
        state["status"] = "paused"
        state["inFlight"] = []
        save_run(state)
        emit(state, "RunPaused", {})
        return state

    while state["queue"] and state.get("status") == "running":
        absorb_signals(state)
        if state.get("pauseRequested"):
            return park_pause()

        ran = set(state["ran"])
        satisfied = set(state["satisfied"])
        ready = [
            node_id
            for node_id in state["queue"]
            if all(pred in ran or pred in satisfied or pred not in steps for pred in preds(scenario, node_id))
        ]
        if not ready:
            break

        state["queue"] = [node_id for node_id in state["queue"] if node_id not in ready]
        state["inFlight"] = list(ready)
        save_run(state)
        routed: list[str] = []
        halted = False

        ready_by_kind: dict[str, list[str]] = {}
        for node_id in ready:
            step = steps.get(node_id)
            if step:
                ready_by_kind.setdefault(kind_of(step), []).append(node_id)

        triggers = ready_by_kind.get("trigger", [])
        agents = ready_by_kind.get("agent", [])
        gates = ready_by_kind.get("gate", [])
        waits = ready_by_kind.get("wait", [])
        humans = ready_by_kind.get("human", [])

        for node_id in triggers:
            step = steps[node_id]
            iteration = int((state["take"].get(node_id) or 0))
            emit(state, "NodePending", {"nodeId": node_id, "iteration": iteration})
            _run_trigger(state, step, iteration)
            routed.extend(succs(scenario, node_id))
            state["inFlight"] = [x for x in state["inFlight"] if x != node_id]

        if agents:
            extra, stop = _run_agents(state, [steps[n] for n in agents], fn)
            routed.extend(extra)
            halted = halted or stop
            state["inFlight"] = [x for x in state["inFlight"] if x not in agents]

        absorb_signals(state)
        if state.get("pauseRequested"):
            for nxt in routed:
                if nxt in steps and nxt not in state["queue"]:
                    state["queue"].append(nxt)
            return park_pause()

        for node_id in gates:
            step = steps[node_id]
            iteration = int((state["take"].get(node_id) or 0))
            emit(state, "NodePending", {"nodeId": node_id, "iteration": iteration})
            extra, stop = _run_gate(state, step, iteration, fn)
            routed.extend(extra)
            if stop:
                halted = True
            state["inFlight"] = [x for x in state["inFlight"] if x != node_id]

        parked = False
        for node_id in waits + humans:
            step = steps[node_id]
            iteration = int((state["take"].get(node_id) or 0))
            emit(state, "NodePending", {"nodeId": node_id, "iteration": iteration})
            if kind_of(step) == "wait":
                if park_wait(state, step, iteration):
                    parked = True
                else:
                    routed.extend(succs(scenario, node_id))
            else:
                park_human(state, step, iteration)
                parked = True
            state["inFlight"] = [x for x in state["inFlight"] if x != node_id]
            if parked:
                for rest in state["inFlight"]:
                    if rest not in state["queue"]:
                        state["queue"].append(rest)
                state["inFlight"] = []
                save_run(state)
                return state

        for nxt in routed:
            if nxt in steps and nxt not in state["queue"]:
                state["queue"].append(nxt)

        state["inFlight"] = []
        save_run(state)
        if halted:
            state["failed"] = True
            break

    if state.get("park") or state.get("status") == "paused":
        save_run(state)
        return state

    leftover = [node_id for node_id in state["queue"] if node_id not in state["ran"]]
    if leftover and not state.get("failed"):
        state["failed"] = True
        _record_failure(
            state,
            leftover[0],
            f"never became ready (still waiting on {', '.join(leftover)})",
        )
        emit(
            state,
            "NodeFailed",
            {
                "nodeId": leftover[0],
                "iteration": int((state.get("take") or {}).get(leftover[0]) or 0),
                "error": f"never became ready (still waiting on {', '.join(leftover)})",
            },
        )

    state["status"] = "failed" if state.get("failed") else "succeeded"
    save_run(state)
    emit(state, "RunFinished", {"state": "failed" if state.get("failed") else "succeeded"})
    return load_run(run_id) or state


def _run_trigger(state: dict, step: dict, iteration: int) -> None:
    node_id = step["id"]
    on = config_of(step).get("on") or {"type": "manual", "spec": ""}
    label = f"{on.get('type') or 'manual'}"
    if on.get("spec"):
        label += f" · {on['spec']}"
    emit(
        state,
        "NodeStarted",
        {"nodeId": node_id, "iteration": iteration, "input": label, "maxIters": 0},
    )
    emit(state, "NodeFinished", {"nodeId": node_id, "iteration": iteration})
    state["ran"].append(node_id)
    state["take"][node_id] = iteration + 1
    state["verdicts"][node_id] = None


def _arm_matches(arm: dict, inputs: list[dict], state: dict, execute_fn: ExecuteFn | None) -> bool:
    when = arm.get("when") or {}
    if not isinstance(when, dict):
        # Validated at start_run; this is the belt to that braces, so a gate
        # reached by some other path still fails as a graph error rather than
        # an AttributeError from the middle of a run.
        raise WorkflowGraphError(
            f"gate arm {arm.get('id')!r} has a {type(when).__name__} 'when'; "
            "expected an object like {'mode': 'all-pass'}"
        )
    if when.get("mode") != "prose":
        return holds(when, inputs)
    source = str(when.get("source") or "").strip() or "Should this arm be taken? Answer PASS or FAIL."
    context = "\n".join(
        f"{item['nodeId']}: {item.get('verdict') or '—'} · {(state.get('summaries') or {}).get(item['nodeId'], '')}"
        for item in inputs
    )
    if execute_fn is None:
        from wfgraph.agent import execute_agent_step

        result = execute_agent_step(source, context, state.get("payload"), {"maxIterations": 8})
    else:
        result = execute_fn(source, context, state.get("payload"), {"maxIterations": 8})
    if not result.get("ok", True):
        return False
    verdict = result.get("verdict")
    if verdict:
        return verdict == "PASS"
    text = str(result.get("summary") or "").upper()
    return "PASS" in text or text.startswith("YES")


class WorkflowGraphError(ValueError):
    """The graph itself is malformed — an unroutable gate, a dangling arm.

    A ValueError subclass on purpose: start_run's callers already treat
    ValueError as "bad workflow, tell the user", so this reaches them as a
    message rather than a traceback.
    """


# The kinds _advance actually dispatches. A step of any other kind is never
# executed and never routes to its successors, so a run containing one used to
# report "succeeded" having done nothing at all — a typo in `kind` read as a
# green run. Kept next to the dispatch it mirrors.
SUPPORTED_KINDS = ("trigger", "agent", "gate", "wait", "human")


def _reject_malformed_structure(scenario: dict, steps: list[dict]) -> None:
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


def _reject_unparseable_waits(steps: list[dict]) -> None:
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


def _reject_bad_gate_arms(steps: list[dict]) -> None:
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


def _reject_deadlocked_back_edges(scenario: dict, steps: list[dict]) -> None:
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


def _reject_unknown_kinds(steps: list[dict]) -> None:
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


def _run_gate(state: dict, step: dict, iteration: int, execute_fn: ExecuteFn | None = None) -> tuple[list[str], bool]:
    node_id = step["id"]
    scenario = state["scenario"]
    inputs = [{"nodeId": pred, "verdict": state["verdicts"].get(pred)} for pred in preds(scenario, node_id)]
    emit(
        state,
        "NodeStarted",
        {
            "nodeId": node_id,
            "iteration": iteration,
            "input": " · ".join(f"{i['nodeId']} {i['verdict'] or '—'}" for i in inputs) or "no inputs",
            "maxIters": 8,
        },
    )
    arms = config_of(step).get("arms") or []
    for _arm in arms:
        if isinstance(_arm, dict) and not str(_arm.get("id") or "").strip():
            raise WorkflowGraphError(
                f"Gate '{node_id}' has an arm with no id. An arm's id selects the "
                "outgoing edge by its sourceHandle; without one the arm matches "
                "every successor and the gate silently takes the first edge "
                "regardless of its verdict. Give every arm an id."
            )
    arm = next((a for a in arms if isinstance(a, dict) and _arm_matches(a, inputs, state, execute_fn)), None)
    route = None
    if arm is not None:
        arm_id = str(arm.get("id")).strip()
        targets = succs(scenario, node_id, arm_id)
        if not targets:
            raise WorkflowGraphError(
                f"Gate '{node_id}' matched arm '{arm_id}' but no edge leaves "
                f"'{node_id}' with sourceHandle '{arm_id}'. Connect the arm, or "
                "the gate's decision cannot be acted on."
            )
        route = targets[0]
    culprit = next((i for i in inputs if i.get("verdict") == "FAIL"), None)
    decision = "fail" if culprit else "pass"
    title = title_of(by_id(scenario).get(route) or {"id": route or "", "title": "nowhere"})
    emit(
        state,
        "GateEvaluated",
        {
            "nodeId": node_id,
            "iteration": iteration,
            "inputs": inputs,
            "decision": decision,
            "route": route or "",
            "summary": f"{culprit['nodeId'] + ' FAIL' if culprit else 'group PASS'} → {title}",
        },
    )
    state["ran"].append(node_id)
    state["take"][node_id] = iteration + 1
    state["verdicts"][node_id] = "FAIL" if culprit else "PASS"

    if not route:
        emit(
            state,
            "NodeFailed",
            {
                "nodeId": node_id,
                "iteration": iteration,
                "error": (
                    f'"{arm.get("label") or arm.get("id")}" isn\'t wired anywhere'
                    if arm
                    else "no arm matched, so the work has nowhere to go"
                ),
            },
        )
        state["failed"] = True
        return [], True

    if route in state["ran"]:
        cap = int(config_of(step).get("maxLoops") or 5)
        if state["loops"] >= cap:
            emit(state, "NodeFailed", {"nodeId": node_id, "iteration": iteration, "error": f"gave up after {cap} takes"})
            _record_failure(state, node_id, f"gave up after {cap} takes")
            state["failed"] = True
            return [], True
        state["loops"] += 1
        emit(
            state,
            "LoopAdvanced",
            {
                "loopId": node_id,
                "iteration": state["loops"],
                "to": route,
                "feedback": f"{culprit['nodeId']} feedback" if culprit else "another take",
            },
        )
        body = between(scenario, route, node_id)
        rerun = []
        for item in body:
            if item in state["ran"]:
                state["ran"] = [x for x in state["ran"] if x != item]
            if item not in {route, node_id} and state["verdicts"].get(item) == "PASS":
                if item not in state["satisfied"]:
                    state["satisfied"].append(item)
                emit(
                    state,
                    "NodeSkipped",
                    {
                        "nodeId": item,
                        "iteration": state["loops"],
                        "reason": f"satisfied · PASS on take {state['take'].get(item) or 1}",
                    },
                )
            else:
                rerun.append(item)
        return [route], False

    return [route], False


def _record_failure(state: dict, node_id: str, error: str) -> None:
    """Persist why a step failed onto the run record itself.

    ``emit`` publishes to the live event stream, which is gone by the time a
    cron or webhook run is inspected on disk. Without this the stored run says
    ``failed`` with no reason, which is unactionable in production.
    """
    entry = {"nodeId": node_id, "error": error}
    errors = state.setdefault("errors", [])
    if isinstance(errors, list):
        errors.append(entry)
    if not state.get("error"):
        state["error"] = f"{node_id}: {error}"


def _compute_agent(
    state: dict,
    step: dict,
    iteration: int,
    execute_fn: ExecuteFn | None,
    session_id: str,
    resume: bool,
) -> dict:
    """Run one agent step. Called from a pool thread under fan-out, so it must
    treat ``state`` as read-only: the session id and resume flag are decided by
    the caller on the main thread (see _assign_sessions) precisely so this does
    no read-modify-write on shared state and never writes the run file."""
    node_id = step["id"]
    cfg = config_of(step)
    goal = str(cfg.get("goal") or "").strip() or title_of(step)
    context = "" if cfg.get("blind") else _context_for(state, node_id)
    traces: list[tuple[str, str]] = []

    def on_tool(name: str, arg: str = "") -> None:
        traces.append((name, arg))

    if state.get("fake") and execute_fn is None:
        return fake.play(state, step, iteration)
    if execute_fn is None:
        from wfgraph.agent import execute_agent_step

        result = execute_agent_step(
            goal,
            context,
            state.get("payload"),
            cfg,
            on_tool=on_tool,
            session_id=session_id,
            resume=resume or bool(state.get("tries", {}).get(node_id)),
        )
    else:
        result = execute_fn(goal, context, state.get("payload"), cfg)
    if not isinstance(result, dict):
        # execute_fn reaches a live model in production, and the code around it
        # returns junk sometimes - None on a swallowed timeout, a bare string
        # from a mis-wired provider shim. Left alone this surfaced deeper in as
        # "AttributeError: 'NoneType' object has no attribute 'get'": a
        # traceback pointing at engine internals for an agent-side fault.
        raise TypeError(
            f"step {node_id!r}: agent returned {type(result).__name__}, "
            f"expected a dict like {{'ok': True, 'verdict': 'PASS'}} (got {result!r})"
        )
    if traces:
        result = {**result, "_traces": traces}
    return result


def _apply_agent(state: dict, step: dict, iteration: int, result: dict) -> str:
    node_id = step["id"]
    cfg = config_of(step)
    for name, arg in result.get("_traces") or []:
        emit(
            state,
            "AgentTraceEvent",
            {"nodeId": node_id, "iteration": iteration, "tool": {"name": name, "arg": arg}},
        )
    if result.get("_paused"):
        state["pauseRequested"] = True
        return "hold"
    if not result.get("ok", True):
        error = str(result.get("error") or "step failed")
        emit(state, "NodeFailed", {"nodeId": node_id, "iteration": iteration, "error": error})
        # emit() only reaches the live event stream. A cron or webhook run is
        # read back from disk long after that stream is gone, so the reason has
        # to live on the run record or the operator sees "failed" and nothing.
        _record_failure(state, node_id, error)
        from wfgraph.agent import is_user_fixable

        if is_user_fixable(error):
            emit(state, "UserAsk", {"nodeId": node_id, "prompt": error})
            state["failed"] = True
            return "halt"
        retries = int(state.setdefault("tries", {}).get(node_id) or 0)
        allowed = int(cfg.get("maxRetries") or 0)
        if retries < allowed:
            state["tries"][node_id] = retries + 1
            return "retry"
        state["ran"].append(node_id)
        state["take"][node_id] = iteration + 1
        state["verdicts"][node_id] = "FAIL"
        on_fail = cfg.get("onFail") or "halt"
        if on_fail == "route":
            return "route"
        state["failed"] = True
        return "halt"

    summary = str(result.get("summary") or "done")
    verdict = result.get("verdict")
    output = result.get("output") if isinstance(result.get("output"), dict) else {"text": summary}
    emit(state, "AgentTraceSummary", {"nodeId": node_id, "iteration": iteration, "summary": summary, "verdict": verdict})
    emit(state, "TaskOutput", {"nodeId": node_id, "iteration": iteration, "output": output})
    emit(state, "NodeFinished", {"nodeId": node_id, "iteration": iteration})
    state["ran"].append(node_id)
    state["take"][node_id] = iteration + 1
    state["verdicts"][node_id] = verdict
    state["summaries"][node_id] = summary
    state["outputs"][node_id] = output
    return "ok"


def _assign_sessions(state: dict, prepared: list[tuple[dict, int]]) -> dict[str, tuple[str, bool]]:
    """Claim a session id per node on the main thread, before any worker runs.

    Workers used to do this themselves — ``state.setdefault("sessions", {})``
    followed by ``save_run(state)`` from inside the pool — an unguarded
    read-modify-write on a dict being serialized by up to eight threads at
    once. Deciding here means the pool only ever reads.
    """
    sessions = state.setdefault("sessions", {})
    tries = state.get("tries") or {}
    plan: dict[str, tuple[str, bool]] = {}
    for step, _iteration in prepared:
        node_id = step["id"]
        existing = sessions.get(node_id)
        session_id = existing or f"wf-{state['runId']}-{node_id}"
        sessions[node_id] = session_id
        plan[node_id] = (session_id, bool(existing) or bool(tries.get(node_id)))
    save_run(state)
    return plan


def _run_agents(state: dict, steps: list[dict], execute_fn: ExecuteFn | None) -> tuple[list[str], bool]:
    routed: list[str] = []
    halted = False
    prepared = []
    for step in steps:
        iteration = int((state["take"].get(step["id"]) or 0))
        cfg = config_of(step)
        goal = str(cfg.get("goal") or "").strip() or title_of(step)
        emit(state, "NodePending", {"nodeId": step["id"], "iteration": iteration})
        emit(
            state,
            "NodeStarted",
            {
                "nodeId": step["id"],
                "iteration": iteration,
                "input": goal[:80],
                "maxIters": int(cfg.get("maxIterations") or 20),
                "loop": iteration > 0,
            },
        )
        prepared.append((step, iteration))

    results: dict[str, dict] = {}
    plan = _assign_sessions(state, prepared)
    if len(prepared) == 1:
        step, iteration = prepared[0]
        session_id, resume = plan[step["id"]]
        results[step["id"]] = _compute_agent(state, step, iteration, execute_fn, session_id, resume)
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(prepared))) as pool:
            futs = {
                pool.submit(
                    _compute_agent, state, step, iteration, execute_fn, *plan[step["id"]]
                ): step["id"]
                for step, iteration in prepared
            }
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()

    for step, iteration in prepared:
        stop = _apply_agent(state, step, iteration, results[step["id"]])
        if stop == "halt":
            halted = True
        elif stop == "retry":
            state["queue"].append(step["id"])
        elif stop == "hold":
            if step["id"] not in state["queue"]:
                state["queue"].append(step["id"])
        else:
            routed.extend(succs(state["scenario"], step["id"]))
    return routed, halted


def respond(run_id: str, node_id: str, decision: str, *, by: str | None = None, execute_fn: ExecuteFn | None = None) -> dict:
    with lock_for(run_id):
        state = load_run(run_id)
        if state is None:
            raise ValueError(f"No run '{run_id}'.")
        park = state.get("park") or {}
        if park.get("kind") != "human" or park.get("nodeId") != node_id:
            raise ValueError("This run is not waiting on that person.")
        choice = "approved" if decision == "approved" else "denied"
        who = by or park.get("who") or "you"
        iteration = int(park.get("iteration") or 0)
        emit(state, "HumanResponded", {"nodeId": node_id, "iteration": iteration, "decision": choice, "by": who})
        state["park"] = None
        if choice == "approved":
            state["ran"].append(node_id)
            state["take"][node_id] = iteration + 1
            state["verdicts"][node_id] = "PASS"
            state["status"] = "running"
            for nxt in succs(state["scenario"], node_id):
                if nxt not in state["queue"]:
                    state["queue"].append(nxt)
            save_run(state)
        else:
            on_fail = park.get("onFail") or "halt"
            if on_fail == "retry":
                state["status"] = "running"
                if node_id not in state["queue"]:
                    state["queue"].append(node_id)
                save_run(state)
            else:
                state["failed"] = True
                state["ran"].append(node_id)
                state["take"][node_id] = iteration + 1
                state["verdicts"][node_id] = "FAIL"
                state["status"] = "failed"
                save_run(state)
                emit(state, "RunFinished", {"state": "failed"})
                return load_run(run_id) or state
    return advance(run_id, execute_fn=execute_fn)


def resolve_event(
    event: str,
    payload: Any = None,
    *,
    background: bool = True,
    execute_fn: ExecuteFn | None = None,
) -> list[str]:
    """Resume every run parked on this event name. Payload is recorded on the wait."""
    needle = (event or "").strip().lower()
    resumed: list[str] = []
    if not needle:
        return resumed
    for state in list_runs():
        if state.get("status") != "waiting_world":
            continue
        waiting = str(state.get("waitingEvent") or "").strip().lower()
        if waiting != needle:
            continue
        park = state.get("park") or {}
        if park.get("kind") != "wait":
            continue
        with lock_for(state["runId"]):
            live = load_run(state["runId"])
            if live is None or live.get("status") != "waiting_world":
                continue
            if payload is not None:
                live["payload"] = payload
            node_id = park["nodeId"]
            finish_wait(live, node_id, int(park.get("iteration") or 0), "event received")
            for nxt in succs(live["scenario"], node_id):
                if nxt not in live["queue"]:
                    live["queue"].append(nxt)
            save_run(live)
        if background:
            spawn(state["runId"], execute_fn)
        else:
            advance(state["runId"], execute_fn=execute_fn)
        resumed.append(state["runId"])
    return resumed


def request_pause(run_id: str) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    if state.get("status") != "running":
        return state
    if state.get("park"):
        return state
    # Same cross-process rule as start_run: a pause request arriving from a
    # CLI process must not declare the gateway's live run dead.
    if not owner_alive(state):
        return fail_dead_run(state)
    signal(run_id, "pause")
    state["pauseRequested"] = True
    save_run(state)
    return state


def resume_run(run_id: str, *, execute_fn: ExecuteFn | None = None) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    if state.get("status") != "paused":
        return state
    clear_signal(run_id)
    state["pauseRequested"] = False
    state["status"] = "running"
    save_run(state)
    if execute_fn is not None:
        return advance(run_id, execute_fn=execute_fn)
    spawn(run_id)
    return load_run(run_id) or state


def cancel_run(run_id: str) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    signal(run_id, "cancel")
    state["status"] = "cancelled"
    state["park"] = None
    state["queue"] = []
    save_run(state)
    emit(state, "RunFinished", {"state": "failed"})
    return load_run(run_id) or state


def rearm_parked() -> None:
    """On gateway start: resume running work, wake due timers, leave humans parked."""
    tick_timers()
    tick_polls()
    for state in list_runs():
        status = state.get("status")
        if status == "running":
            spawn(state["runId"])
        elif status == "waiting_world":
            rearm(state)


def snapshot(run_id: str, after: int = -1) -> dict:
    state = load_run(run_id)
    if state is None:
        raise ValueError(f"No run '{run_id}'.")
    return {"run": state, "events": load_events(run_id, after)}


def snapshot_active(workflow_id: str) -> dict | None:
    state = active_run(workflow_id)
    if state is None:
        return None
    return {"run": state, "events": load_events(state["runId"])}
