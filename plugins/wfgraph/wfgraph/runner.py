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
from wfgraph import receipt as receipt_states
from wfgraph.lease import owner_alive
from wfgraph.lease import stamp as lease_stamp
from wfgraph.receipt import attach_receipt
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
    edges_of,
    holds,
    is_loop,
    kind_of,
    parse_wait_seconds,
    preds,
    scenario_of,
    steps_of,
    succs,
    title_of,
)
from wfgraph.validate import (
    WorkflowGraphError,
    reject_bad_gate_arms,
    reject_deadlocked_back_edges,
    reject_malformed_structure,
    reject_unknown_kinds,
    reject_unparseable_waits,
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
    reject_malformed_structure(scenario, steps)
    reject_unknown_kinds(steps)
    reject_bad_gate_arms(steps)
    reject_unparseable_waits(steps)
    reject_deadlocked_back_edges(scenario, steps)
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
                # Without this the run persists as "failed" with error=None --
                # the same unactionable state defect #12 fixed for step-level
                # failures. An operator reading a cron/webhook run on disk has
                # only this record; the exception itself is gone.
                _record_failure(live, live.get("at") or "run", str(exc))
                _attach_receipt(live)
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
    # Adopting, not reporting: an abandoned run is exactly what this path
    # recovers, so it must see the raw record rather than a reaped one.
    state = load_run(run_id, reap_orphans=False)
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
        # `satisfied` lets a rework take skip a step whose PASS still stands.
        # It answers "does this need to run again", not "has this finished" --
        # and a step can be both satisfied and queued for the next take. Reading
        # it as finished let a gate start in the same pass as the step feeding
        # it, judging the previous take's verdict while the new one ran beside
        # it. A predecessor still waiting its turn is not done.
        pending = set(state["queue"])
        satisfied = set(state["satisfied"]) - pending
        ready = [
            node_id
            for node_id in state["queue"]
            if all(
                pred in ran or pred in satisfied or pred not in steps
                for pred in preds(scenario, node_id)
            )
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

    # A cancel is a decision, not a fault. It lands on the state before the
    # loop exits, and the leftover queue below is its expected aftermath --
    # relabelling it "failed" would blame the run for stopping when asked.
    #
    # This is the *signal* path: `cancel_run` finalises a run it owns, but a
    # cancel raised while this walk is in flight is absorbed by the loop and
    # lands here instead. It ends the run, so it owes the same receipt and the
    # same RunFinished event -- without them a cancelled run has no finish time
    # and the event stream just stops mid-walk.
    if state.get("status") == "cancelled":
        if not isinstance(state.get("receipt"), dict):
            attach_receipt(
                state,
                outcome=receipt_states.CANCELLED,
                meaning=receipt_states.CANCELLED_BY_REQUEST,
            )
            save_run(state)
            emit(state, "RunFinished", {"state": "cancelled"})
        else:
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
    _attach_receipt(state)
    save_run(state)
    emit(state, "RunFinished", {"state": "failed" if state.get("failed") else "succeeded"})
    return load_run(run_id) or state


def _attach_receipt(state: dict) -> None:
    """Stamp the finish record used by cron, webhooks, and the status tool.

    The graph finished its own walk. Every other terminal path (owner died,
    cancelled, reaped) stamps the same record through the same builder --
    see wfgraph.receipt for what the fields do and do not claim.
    """
    failed = bool(state.get("failed"))
    attach_receipt(
        state,
        outcome=receipt_states.FAILED if failed else receipt_states.DONE,
        meaning=(
            receipt_states.FINISHED_FAILED if failed else receipt_states.FINISHED_CLEANLY
        ),
    )


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




# The kinds _advance actually dispatches. A step of any other kind is never
# executed and never routes to its successors, so a run containing one used to
# report "succeeded" having done nothing at all — a typo in `kind` read as a
# green run. Kept next to the dispatch it mirrors.


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

    # Is this arm a rework loop, or just a forward step? The scenario says so
    # outright -- the author marks the rework edge {"loop": True} and the rest
    # of the engine already honours it (topology.preds skips loop edges when
    # deciding readiness). This used to ask `route in state["ran"]` instead,
    # which is a different question: a forward target can already have run,
    # and on a gate that evaluates more than once (two arms fanning back in)
    # the final step got re-run as though it were rework -- shipping twice and
    # spending a maxLoops budget meant for retries.
    loops_back = any(
        edge["target"] == route and is_loop(edge)
        for edge in edges_of(scenario)
        if edge["source"] == node_id
    )
    if loops_back:
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
                attach_receipt(
                    state,
                    outcome=receipt_states.FAILED,
                    meaning=receipt_states.DENIED_BY_PERSON,
                )
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
    attach_receipt(
        state,
        outcome=receipt_states.CANCELLED,
        meaning=receipt_states.CANCELLED_BY_REQUEST,
    )
    save_run(state)
    # The event carries the run's own outcome. It used to say "failed" while
    # the record said "cancelled", so a canvas or log reading the stream
    # reported a broken workflow every time a user stopped one on purpose.
    emit(state, "RunFinished", {"state": "cancelled"})
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
