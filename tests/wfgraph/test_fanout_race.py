"""Fan-out must not corrupt the run it is writing into.

Parallel agent nodes each used to reach into the shared run dict, mutate
state["sessions"], and call save_run() from their own pool thread. Nothing
guarded that read-modify-write, so concurrent nodes could drop each other's
session ids and serialize a half-updated dict. The engine now assigns sessions
on the main thread before any worker starts.
"""

import threading

from wfgraph.runner import start_run
from wfgraph.store import load_run, save_documents

WIDTH = 8


def _scenario(*steps, edges=None):
    return {"steps": list(steps), "edges": list(edges or [])}


def _fan_out_doc(width=WIDTH):
    steps = [{"id": "t", "kind": "trigger", "config": {"title": "T", "on": {"type": "manual", "spec": ""}}}]
    edges = []
    for i in range(width):
        steps.append({"id": f"a{i}", "kind": "agent", "config": {"title": f"A{i}", "goal": "go"}})
        edges.append({"id": f"e{i}", "source": "t", "target": f"a{i}"})
    return {"id": "fan", "name": "fan", "scenario": _scenario(*steps, edges=edges)}


def _put(monkeypatch, tmp_path, doc):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    save_documents([doc], doc["id"])


def test_parallel_agents_all_run_and_keep_distinct_sessions(tmp_path, monkeypatch):
    """Every fanned-out node must run exactly once and keep its own session id.

    The barrier forces all workers to be inside the step simultaneously, which
    is what makes the interleaving actually happen rather than hoping for it.
    """
    _put(monkeypatch, tmp_path, _fan_out_doc())

    barrier = threading.Barrier(WIDTH, timeout=10)
    seen = []
    seen_lock = threading.Lock()

    def concurrent_agent(goal, _context, _payload, _cfg):
        barrier.wait()
        with seen_lock:
            seen.append(goal)
        return {"ok": True, "summary": "done", "verdict": "PASS", "output": {}}

    state = start_run(
        "fan", payload=None, source="manual",
        execute_fn=concurrent_agent, background=False,
    )

    assert state["status"] == "succeeded", state.get("status")
    assert len(seen) == WIDTH, f"expected {WIDTH} concurrent agents, saw {len(seen)}"

    saved = load_run(state["runId"])
    sessions = saved["sessions"]
    expected = {f"a{i}" for i in range(WIDTH)}

    assert set(sessions) == expected, f"lost session rows under fan-out: {set(sessions) ^ expected}"
    assert len(set(sessions.values())) == WIDTH, "session ids collided across parallel nodes"
    assert set(saved["ran"]) >= expected, "a fanned-out node never ran"


def test_workers_do_not_write_the_run_file(tmp_path, monkeypatch):
    """The invariant behind the fix: no save_run() from a pool thread.

    Each worker records the thread it ran on; the run file must only ever be
    written by the main thread that owns the state dict.
    """
    _put(monkeypatch, tmp_path, _fan_out_doc())

    main_thread = threading.current_thread().ident
    writes_off_main = []
    import wfgraph.runner as runner
    import wfgraph.store as store

    real_save = store.save_run

    def watched_save(state):
        if threading.current_thread().ident != main_thread:
            writes_off_main.append(threading.current_thread().name)
        return real_save(state)

    monkeypatch.setattr(store, "save_run", watched_save)
    monkeypatch.setattr(runner, "save_run", watched_save)

    start_run(
        "fan", payload=None, source="manual",
        execute_fn=lambda *a, **k: {"ok": True, "verdict": "PASS", "output": {}},
        background=False,
    )

    assert not writes_off_main, f"run file written from pool threads: {writes_off_main}"
