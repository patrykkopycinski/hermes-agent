"""es_memory's declared config surface and its graceful degradation.

Two contracts worth pinning separately from the provider tests: the schema module must load
without the agent runtime (the web server loads it by path), and a missing ``elasticsearch``
package must cost the provider its session, never the agent.
"""
from __future__ import annotations

import ast
import builtins
import json

import pytest

import plugins.memory.es_memory as es_memory
from plugins.memory import find_provider_dir
from plugins.memory.config_schema import KIND_SECRET, STORAGE_FLAT_JSON, get_provider_config_schema
from plugins.memory.es_memory import _STRATEGIES, EsMemoryProvider
from pm.extras import ANCHORS


# -- declared schema ------------------------------------------------------


def test_schema_loads_through_the_generic_loader():
    schema = get_provider_config_schema("es_memory")

    assert schema is not None
    # The flat-JSON writer derives $HERMES_HOME/<name>/config.json from this, and the loader
    # finds the module by directory name — a mismatch silently splits reads from writes.
    assert schema.name == "es_memory"
    assert schema.storage == STORAGE_FLAT_JSON


def test_credentials_are_secret_fields_backed_by_env_keys():
    fields = {field.key: field for field in get_provider_config_schema("es_memory").fields}

    for key, env_key in (("api_key", "ES_MEMORY_API_KEY"), ("password", "ES_MEMORY_PASSWORD")):
        assert fields[key].kind == KIND_SECRET
        assert fields[key].env_key == env_key


def test_retrieval_options_match_the_strategies_the_provider_implements():
    retrieval = next(f for f in get_provider_config_schema("es_memory").fields if f.key == "retrieval")

    assert retrieval.allowed_values() == set(_STRATEGIES)
    assert retrieval.default == "auto"


def test_schema_module_does_not_import_the_agent_runtime():
    """``config_schema.py`` is loaded by path inside the web server, which must not pull in
    the agent runtime. Importing anything but the pure-data module breaks that."""
    source = (find_provider_dir("es_memory") / "config_schema.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }

    assert imported == {"plugins.memory.config_schema"}


def test_declared_and_setup_schemas_agree_on_keys():
    """``hermes memory setup`` and the dashboard must offer the same knobs."""
    declared = {field.key for field in get_provider_config_schema("es_memory").fields}
    setup = {field["key"] for field in EsMemoryProvider().get_config_schema()}

    assert declared == setup


# -- graceful degradation -------------------------------------------------


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ES_MEMORY_URL", "https://es.example:9243")
    monkeypatch.setenv("ES_MEMORY_API_KEY", "test-key")


def _block_elasticsearch_import(monkeypatch):
    real_import = builtins.__import__

    def _guarded(name, *args, **kwargs):
        if name == "elasticsearch" or name.startswith("elasticsearch."):
            raise ImportError("No module named 'elasticsearch'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guarded)


def test_available_even_when_the_sdk_is_absent(configured, monkeypatch):
    """The SDK lazy-installs during initialize(); gating availability on the import would
    mean it never gets installed on a sealed venv."""
    _block_elasticsearch_import(monkeypatch)

    assert EsMemoryProvider().is_available() is True


def test_initialize_degrades_quietly_without_the_sdk(tmp_path, configured, monkeypatch):
    monkeypatch.setattr("pm.ensure_import", lambda *a, **k: None, raising=False)
    _block_elasticsearch_import(monkeypatch)

    provider = EsMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")

    assert provider._active is False
    # Every downstream call must be a no-op rather than a crash.
    provider.sync_turn("q", "a")
    provider.on_memory_write("add", "user", "a fact")
    provider.queue_prefetch("anything")
    assert provider.prefetch("anything") == ""
    assert provider.recall_status() is None
    provider.shutdown()


def test_initialize_degrades_quietly_when_the_cluster_is_unreachable(tmp_path, configured, monkeypatch):
    def _explode(self):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(es_memory._EsClient, "connect", _explode)

    provider = EsMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")

    assert provider._active is False
    assert provider.prefetch("anything") == ""


def test_initialize_degrades_quietly_when_index_bootstrap_fails(tmp_path, configured, monkeypatch):
    monkeypatch.setattr(es_memory._EsClient, "connect", lambda self: setattr(self, "_client", object()))
    monkeypatch.setattr(es_memory._EsClient, "ensure_index",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403 forbidden")))

    provider = EsMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")

    assert provider._active is False


def test_tool_call_reports_the_configuration_problem(tmp_path, configured, monkeypatch):
    monkeypatch.setattr(es_memory._EsClient, "connect",
                        lambda self: (_ for _ in ()).throw(ConnectionError("refused")))
    provider = EsMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")

    result = json.loads(provider.handle_tool_call("es_memory_search", {"query": "anything"}))

    assert result.get("error")


def test_lazy_dependency_is_allowlisted():
    """The SDK only installs on a sealed image if the extra is anchored for pm.ensure_import."""
    assert ANCHORS["es-memory"] == "elasticsearch"
