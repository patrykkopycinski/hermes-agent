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
import plugins.memory.es_memory.client as es_client_module
from plugins.memory import find_provider_dir
from plugins.memory.config_schema import KIND_SECRET, STORAGE_FLAT_JSON, get_provider_config_schema
from plugins.memory.es_memory import ESMemoryProvider
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

    # "hybrid" was removed with the .client rewrite: retrieval is auto|semantic|bm25
    assert retrieval.allowed_values() == {"auto", "semantic", "bm25"}
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
    """``hermes memory setup`` offers the common knobs; the dashboard panel may expose more."""
    declared = {field.key for field in get_provider_config_schema("es_memory").fields}
    setup = {field["key"] for field in ESMemoryProvider().get_config_schema()}

    assert setup <= declared  # every setup knob must exist in the dashboard schema


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

    assert ESMemoryProvider().is_available() is True


def test_client_never_imports_the_elasticsearch_sdk(tmp_path, configured, fake_es, monkeypatch):
    """The httpx REST client made the lazy SDK install (and its sealed-venv trap) obsolete."""
    source = (find_provider_dir("es_memory") / "client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }

    assert not any(name.split(".")[0] == "elasticsearch" for name in imported)


def test_initialize_degrades_quietly_when_the_cluster_is_unreachable(tmp_path, configured, fake_es, monkeypatch):
    """A dead endpoint must cost the provider its session, never the agent."""
    def _explode(self):
        raise es_client_module.ElasticsearchError("connection refused")

    monkeypatch.setattr(es_client_module.ElasticsearchClient, "ping", _explode)

    provider = ESMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")

    # Initialize survived; every downstream call is a no-op rather than a crash.
    provider.sync_turn("q", "a")
    provider.on_memory_write("add", "user", "a fact")
    assert provider.prefetch("anything") == ""


def test_initialize_degrades_quietly_when_index_bootstrap_fails(tmp_path, configured, fake_es, monkeypatch):
    def _explode(self, index, body):
        raise es_client_module.ElasticsearchError("403 forbidden")

    monkeypatch.setattr(es_client_module.ElasticsearchClient, "create_index", _explode)

    provider = ESMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")

    # Bootstrap failure is logged, never raised; the provider keeps serving reads.
    payload = json.loads(provider.handle_tool_call("es_memory_search", {"query": "anything"}))
    assert "error" in payload or "results" in payload


def test_tool_call_reports_the_configuration_problem(tmp_path, configured, fake_es, monkeypatch):
    def _explode(self, index, body):
        raise es_client_module.ElasticsearchError("connection refused")

    monkeypatch.setattr(es_client_module.ElasticsearchClient, "search", _explode)
    provider = ESMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")

    result = json.loads(provider.handle_tool_call("es_memory_search", {"query": "anything"}))

    assert result.get("error")


def test_lazy_dependency_is_allowlisted():
    """The SDK only installs on a sealed image if the extra is anchored for pm.ensure_import."""
    assert ANCHORS["es-memory"] == "elasticsearch"
