"""MCP servers (e.g. envoy-ai-gateway) may send ``"cacheScope": ""`` on list results; the mcp 2.x SDK
types it ``Literal["public", "private"]`` and rejects the whole listing, so zero tools load. Hermes
drops an invalid cacheScope at the session dispatcher so the SDK default ('private') applies."""

import asyncio
import logging

import pytest

pytest.importorskip("mcp")
pytest.importorskip("mcp_types")

from mcp.client.session import ClientSession  # noqa: E402

from tools.mcp_tool_transport import _drop_invalid_cache_scope, _guard_cache_scope  # noqa: E402

_TOOL = {"name": "mcp-ecp-release-info__get_release_status", "inputSchema": {"type": "object"}}


class _FakeDispatcher:
    """Stands in for the SDK JSON-RPC dispatcher: returns a canned raw ``tools/list`` result."""

    def __init__(self, raw):
        self.raw = raw

    async def send_raw_request(self, method, params, opts=None, **kw):
        return dict(self.raw, tools=[dict(t) for t in self.raw["tools"]])


def _list_tools(raw, guard=True):
    session = ClientSession(dispatcher=_FakeDispatcher(raw))
    if guard:
        _guard_cache_scope(session, "srv")
    return asyncio.run(session.list_tools())


@pytest.mark.parametrize("bad", ["", "global", None, 5])
def test_invalid_cache_scope_parses_with_tools_intact(bad):
    result = _list_tools({"tools": [_TOOL], "cacheScope": bad, "ttlMs": 0})
    assert [t.name for t in result.tools] == [_TOOL["name"]]
    assert result.cache_scope == "private"


@pytest.mark.parametrize("scope", ["public", "private"])
def test_valid_cache_scope_preserved(scope):
    result = _list_tools({"tools": [_TOOL], "cacheScope": scope, "ttlMs": 1000})
    assert result.cache_scope == scope
    assert result.ttl_ms == 1000


def test_sdk_still_rejects_without_guard():
    """Premise check: the SDK itself rejects '' — otherwise the guard is untested."""
    with pytest.raises(Exception, match="cacheScope"):
        _list_tools({"tools": [_TOOL], "cacheScope": ""}, guard=False)


def test_guard_is_idempotent_and_logs_server_name(caplog):
    session = ClientSession(dispatcher=_FakeDispatcher({"tools": [_TOOL], "cacheScope": ""}))
    _guard_cache_scope(session, "elastic-mcp-gateway-staging")
    first = session._dispatcher.send_raw_request
    _guard_cache_scope(session, "elastic-mcp-gateway-staging")
    assert session._dispatcher.send_raw_request is first
    with caplog.at_level(logging.DEBUG, logger="tools.mcp_tool"):
        asyncio.run(session.list_tools())
    assert any("elastic-mcp-gateway-staging" in r.getMessage() and "cacheScope" in r.getMessage()
               for r in caplog.records)


def test_drop_only_touches_cache_scope_and_dicts():
    raw = {"tools": [], "cacheScope": "x", "ttlMs": 7, "nextCursor": "c"}
    assert _drop_invalid_cache_scope(raw, "s") == {"tools": [], "ttlMs": 7, "nextCursor": "c"}
    assert _drop_invalid_cache_scope("not-a-dict", "s") == "not-a-dict"
