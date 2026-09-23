"""goal_criteria completion republishes the session-control snapshot mid-turn.

The Desktop goal card repaints from ``session.control.update``. Slash paths
publish on dispatch, and the post-turn hook publishes after the judge — but an
agent-facing ``goal_criteria`` call mutates subgoals/gates in the MIDDLE of a
turn, so without this publish the card keeps the ``/goal``-set snapshot
(``Criteria · 0``) until the turn ends.
"""

import tui_gateway.server as server


def _armed(monkeypatch, sid, **session_extra):
    events = []
    session = {
        "agent": None,
        "edit_snapshots": {},
        "tool_started_at": {},
        "tool_progress_mode": "off",
        **session_extra,
    }
    monkeypatch.setitem(server._sessions, sid, session)
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: False)
    monkeypatch.setattr(server, "_tool_lifecycle_required_for_ui", lambda _name: False)
    monkeypatch.setattr(
        server, "_emit", lambda event, event_sid, payload=None: events.append((event, event_sid, payload))
    )
    return events, session


def test_goal_criteria_completion_publishes_only_if_present(monkeypatch):
    sid = "goal-criteria-only-if-present"
    calls = []
    _armed(monkeypatch, sid)
    monkeypatch.setattr(
        server,
        "_publish_session_control_snapshot",
        lambda *a, **k: calls.append(k),
    )

    server._on_tool_complete(sid, "call-1", "goal_criteria", {"action": "show"}, "ok")

    assert calls == [{"only_if_present": True}]


def test_ordinary_tool_completion_publishes_nothing(monkeypatch):
    sid = "ordinary-tool-no-publish"
    calls = []
    _armed(monkeypatch, sid)
    monkeypatch.setattr(server, "_publish_session_control_snapshot", lambda *a, **k: calls.append(k))

    server._on_tool_complete(sid, "call-1", "terminal", {"command": "pwd"}, "ok")

    assert calls == []


def test_goal_criteria_without_session_key_still_defers_to_publisher(monkeypatch):
    """Missing session_key is handled inside the publisher (returns early); the completion
    hook's own job is just to route goal_criteria completions to it."""
    sid = "goal-criteria-no-session"
    calls = []
    monkeypatch.setitem(server._sessions, sid, {"agent": None, "tool_progress_mode": "off"})
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: False)
    monkeypatch.setattr(server, "_publish_session_control_snapshot", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(server, "_connector_lifecycle_is_stale", lambda *a: False)

    server._on_tool_complete(sid, "call-1", "goal_criteria", {"action": "show"}, "ok")

    assert calls == [{"only_if_present": True}]
