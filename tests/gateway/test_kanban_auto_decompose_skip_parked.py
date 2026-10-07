"""The per-tick auto-decomposer must not fan out triage tasks a human parked.

Regression for the t_89e3a8fb incident: ``_route_block`` escalates a block loop
to ``triage`` "for a human", and ``auto_decompose_tick`` then decomposed it into
children that recreated the concurrent writer the park was meant to stop. Uses a
real kanban DB under a temp HERMES_HOME; only the aux-LLM ``decompose_task`` is faked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kwd, "_board_slugs", lambda kb_mod: ["default"])
    return home


def _claim(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _block_loop_to_triage(conn) -> str:
    tid = kb.create_task(conn, title="parked", assignee="worker")
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
        _claim(conn, tid)
        kb.block_task(conn, tid, reason="sibling writer collision", kind="capability")
        if kb.get_task(conn, tid).status == "blocked":
            kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    assert any(e.kind == "block_loop_detected" for e in kb.list_events(conn, tid))
    return tid


def _ran_then_triage(conn) -> str:
    tid = kb.create_task(conn, title="already ran", assignee="worker")
    _claim(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    assert kb.get_task(conn, tid).block_recurrences == 0
    return tid


def _tick(monkeypatch) -> tuple[int, list[str]]:
    from hermes_cli import kanban_decompose as real

    seen: list[str] = []

    def fake_decompose(task_id, author=None):
        seen.append(task_id)
        return SimpleNamespace(ok=True, fanout=False, child_ids=None, reason=None)

    fake = SimpleNamespace(
        list_auto_decomposable_ids=real.list_auto_decomposable_ids, decompose_task=fake_decompose,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_decompose", fake)
    import hermes_cli
    monkeypatch.setattr(hermes_cli, "kanban_decompose", fake, raising=False)
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    disp = kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)
    return disp.auto_decompose_tick(10), seen


def test_fresh_triage_task_is_auto_decomposed(board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="fresh idea", triage=True)
    count, seen = _tick(monkeypatch)
    assert seen == [tid]
    assert count == 1


def test_block_loop_triage_task_is_skipped(board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = _block_loop_to_triage(conn)
    count, seen = _tick(monkeypatch)
    assert tid not in seen
    assert count == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_triage_task_with_prior_run_is_skipped(board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = _ran_then_triage(conn)
    count, seen = _tick(monkeypatch)
    assert tid not in seen
    assert count == 0


def test_skipped_tasks_do_not_starve_fresh_ones(board, monkeypatch):
    with kbc.connect_closing() as conn:
        _block_loop_to_triage(conn)
        _ran_then_triage(conn)
        fresh = kb.create_task(conn, title="fresh idea", triage=True)
    count, seen = _tick(monkeypatch)
    assert seen == [fresh]
    assert count == 1
