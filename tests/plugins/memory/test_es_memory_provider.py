"""es_memory provider: config resolution, namespace isolation, write/recall roundtrips.

The Elasticsearch client is always the ``FakeElasticsearch`` double served over
``httpx.MockTransport`` — these tests never reach a cluster, but they do exercise
the provider's real request building, JSON decoding and error classification.
Contracts pinned here: config.json/yaml/env precedence, per-profile namespace
partitioning, content-addressed idempotent writes, semantic→BM25 degradation on
an inference-refusing cluster, and fact supersession on memory-tool rewrites.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import plugins.memory.es_memory as es_memory
from plugins.memory.es_memory import ESMemoryProvider, _content_doc_id, _fact_doc_id, _profile_namespace


def _provider(tmp_path, *, session="sess-1", **init_kwargs):
    provider = ESMemoryProvider()
    provider.initialize(session, hermes_home=str(tmp_path), platform="cli", **init_kwargs)
    return provider


def _write_config(tmp_path, values):
    (tmp_path / "es_memory").mkdir(exist_ok=True)
    (tmp_path / "es_memory" / "config.json").write_text(json.dumps(values), encoding="utf-8")


# -- config ---------------------------------------------------------------


def test_config_file_overrides_env(tmp_path, monkeypatch, no_yaml):
    monkeypatch.setenv("ES_MEMORY_URL", "https://from-env:9200")
    monkeypatch.setattr(es_memory, "get_hermes_home", lambda: tmp_path)
    _write_config(tmp_path, {"url": "https://from-file:9200", "retrieval": "semantic", "top_k": 3})

    config = es_memory._resolve_config()

    assert config["url"] == "https://from-file:9200"
    assert config["retrieval"] == "semantic"
    assert config["top_k"] == 3


def test_save_config_merges_into_profile_json(tmp_path):
    ESMemoryProvider().save_config({"index": "first", "top_k": 4}, str(tmp_path))
    ESMemoryProvider().save_config({"top_k": 9}, str(tmp_path))

    stored = json.loads((tmp_path / "es_memory" / "config.json").read_text(encoding="utf-8"))
    assert stored == {"index": "first", "top_k": 9}


# -- namespace isolation --------------------------------------------------


def test_namespace_is_the_profile_directory_name():
    """One index serves every profile; the namespace is the partition."""
    assert _profile_namespace("/home/a/.hermes") == "default"
    assert _profile_namespace("/home/a/.hermes/profiles/bot-evals") == "bot-evals"
    assert _profile_namespace("") == "default"


def test_two_profiles_write_into_disjoint_namespaces(tmp_path, configured, fake_es):
    a = _provider(tmp_path / "profiles" / "alpha")
    b = _provider(tmp_path / "profiles" / "beta")

    assert a._namespace != b._namespace
    assert a._index == b._index  # same physical index, different logical slice


def test_explicit_namespace_config_wins(tmp_path, configured, fake_es):
    _write_config(tmp_path, {"namespace": "custom-slice"})
    provider = _provider(tmp_path / "profiles" / "alpha")

    assert provider._namespace == "custom-slice"


# -- availability ---------------------------------------------------------


def test_unavailable_without_endpoint(monkeypatch, no_yaml):
    monkeypatch.delenv("ES_MEMORY_URL", raising=False)
    monkeypatch.delenv("ES_MEMORY_CLOUD_ID", raising=False)
    monkeypatch.setattr(es_memory, "_load_file_config", lambda: {})

    provider = ESMemoryProvider()

    assert provider.is_available() is False
    assert "Elasticsearch endpoint" in provider.unavailable_reason()


def test_cloud_id_satisfies_the_endpoint_requirement(monkeypatch, no_yaml):
    monkeypatch.delenv("ES_MEMORY_URL", raising=False)
    monkeypatch.setenv("ES_MEMORY_CLOUD_ID", "deployment:abc123")
    monkeypatch.setattr(es_memory, "_load_file_config", lambda: {})

    assert ESMemoryProvider().is_available() is True


# -- index bootstrap ------------------------------------------------------


def test_bootstrap_creates_index_with_semantic_mapping(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    properties = fake_es.indices[provider._index]["mappings"]["properties"]
    assert properties["content_semantic"] == {"type": "semantic_text", "inference_id": ".elser-2-elasticsearch"}
    assert properties["namespace"] == {"type": "keyword"}  # the partition key must be indexed


def test_bootstrap_is_idempotent_for_an_existing_index(tmp_path, configured, fake_es):
    _provider(tmp_path)
    puts_before = [p for m, p in fake_es.requests if m == "PUT"]
    assert len(puts_before) == 1  # one bootstrap create

    _provider(tmp_path)

    puts_after = [p for m, p in fake_es.requests if m == "PUT"]
    assert puts_after == puts_before  # existence HEAD short-circuits the second create


def test_explicit_bm25_never_declares_a_semantic_field(tmp_path, configured, fake_es):
    _write_config(tmp_path, {"retrieval": "bm25"})
    provider = _provider(tmp_path)

    properties = fake_es.indices[provider._index]["mappings"]["properties"]
    assert "content_semantic" not in properties
    assert provider._inference_id == ""


def test_custom_inference_id_reaches_the_mapping(tmp_path, configured, fake_es, monkeypatch):
    monkeypatch.setenv("ES_MEMORY_INFERENCE_ID", "my-elser")
    provider = _provider(tmp_path)

    semantic = fake_es.indices[provider._index]["mappings"]["properties"]["content_semantic"]
    assert semantic["inference_id"] == "my-elser"


# -- episodic writes ------------------------------------------------------


def test_sync_turn_indexes_a_turn_document(tmp_path, configured, fake_es):
    provider = _provider(tmp_path, session="sess-9")

    provider.sync_turn("what is the retention policy?", "30 days.", session_id="sess-9")

    (doc_id, document), = fake_es.documents[provider._index].items()
    assert document["kind"] == "turn"
    assert document["session_id"] == "sess-9"
    assert document["namespace"] == provider._namespace
    assert "retention policy" in document["content"]
    assert document["content_semantic"] == document["content"]  # semantic mirror


def test_sync_turn_is_idempotent_on_retry(tmp_path, configured, fake_es):
    """A retried write (timeout after the cluster stored it) overwrites, never duplicates."""
    provider = _provider(tmp_path)
    provider.sync_turn("question", "answer")
    provider.sync_turn("question", "answer")

    assert len(fake_es.documents[provider._index]) == 1
    assert next(iter(fake_es.documents[provider._index])) == \
        _content_doc_id(provider._namespace, "sess-1", "User: question\nAssistant: answer")


def test_sync_turn_skipped_for_non_primary_contexts(tmp_path, configured, fake_es):
    """Cron and subagent turns are not this profile's conversation history."""
    provider = _provider(tmp_path, agent_context="subagent")

    provider.sync_turn("hello", "hi")

    assert not any(fake_es.documents.values())  # bootstrap may create the index; no doc ever lands


def test_empty_turn_is_not_written(tmp_path, configured, fake_es):
    _provider(tmp_path).sync_turn("", "")
    assert not any(fake_es.documents.values())


def test_write_degrades_to_bm25_when_inference_is_refused(tmp_path, configured, fake_es):
    """Licensed-mapping, unlicensed-inference cluster: the memory is still stored."""
    fake_es.inference_available = False
    provider = _provider(tmp_path)

    provider.sync_turn("question", "answer")

    document = next(iter(fake_es.documents[provider._index].values()))
    assert "content_semantic" not in document
    assert provider._inference_id == ""  # dropped for the rest of the process


# -- memory-tool mirroring ------------------------------------------------


def test_memory_tool_write_mirrors_a_fact(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    provider.on_memory_write("add", "user", "prefers dark mode")

    document = fake_es.documents[provider._index][_fact_doc_id(provider._namespace, "user", "prefers dark mode")]
    assert document["kind"] == "preference"
    assert document["target"] == "user"
    assert document["active"] is True


def test_replace_with_old_text_supersedes_exactly_that_entry(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    provider.on_memory_write("add", "user", "prefers dark mode")
    provider.on_memory_write("add", "user", "prefers light mode")

    provider.on_memory_write("replace", "user", "prefers light mode",
                             metadata={"old_text": "prefers dark mode"})

    docs = fake_es.documents[provider._index]
    assert docs[_fact_doc_id(provider._namespace, "user", "prefers dark mode")]["active"] is False
    assert docs[_fact_doc_id(provider._namespace, "user", "prefers light mode")]["active"] is True


def test_remove_without_old_text_supersedes_the_whole_target(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    provider.on_memory_write("add", "user", "fact one")
    provider.on_memory_write("add", "user", "fact two")

    provider.on_memory_write("remove", "user", "", metadata={})

    docs = fake_es.documents[provider._index]
    assert all(doc["active"] is False for doc in docs.values())


# -- recall ---------------------------------------------------------------


def test_recall_query_is_namespace_scoped_and_excludes_superseded(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    provider.queue_prefetch("anything")
    thread = provider._prefetch_thread
    assert thread is not None  # queue_prefetch only skips when no client
    thread.join(timeout=5)

    body = fake_es.search_bodies[0]
    query = body["query"]["bool"]
    assert {"term": {"namespace": provider._namespace}} in query["filter"]
    assert query["must_not"] == [{"term": {"active": False}}]


def test_recall_honours_top_k(tmp_path, configured, fake_es):
    _write_config(tmp_path, {"top_k": 3})
    provider = _provider(tmp_path)

    provider.queue_prefetch("anything")
    thread = provider._prefetch_thread
    assert thread is not None  # queue_prefetch only skips when no client
    thread.join(timeout=5)

    assert fake_es.search_bodies[0]["size"] == 3


def test_prefetch_serves_the_background_recall(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.documents.setdefault(provider._index, {})["seed"] = {
        "kind": "fact", "content": "cached hit", "namespace": provider._namespace, "active": True}

    provider.queue_prefetch("cached")
    thread = provider._prefetch_thread
    assert thread is not None  # queue_prefetch only skips when no client
    thread.join(timeout=5)

    block = provider.prefetch("cached")
    assert "cached hit" in block
    # Second prefetch: nothing queued, nothing recalled.
    assert provider.prefetch("cached") == ""


# -- tools ----------------------------------------------------------------


def test_tool_schemas_are_exposed(tmp_path, configured, fake_es):
    schemas = _provider(tmp_path).get_tool_schemas()

    assert {s["name"] for s in schemas} >= {"es_memory_search", "es_memory_remember",
                                            "es_memory_list", "es_memory_forget"}


def test_tool_call_returns_results_json(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.documents.setdefault(provider._index, {}).update({
        "a": {"kind": "fact", "content": "a fact", "namespace": provider._namespace, "active": True},
        "b": {"kind": "turn", "content": "a turn", "namespace": provider._namespace, "active": True},
    })

    payload = json.loads(provider.handle_tool_call("es_memory_search", {"query": "fact"}))

    assert payload["count"] == 1
    assert payload["retrieval"] == "semantic+bm25"
    assert payload["results"][0]["content"] == "a fact"


def test_remember_tool_is_idempotent(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    provider.handle_tool_call("es_memory_remember", {"content": "a durable fact"})
    provider.handle_tool_call("es_memory_remember", {"content": "a durable fact"})

    assert len(fake_es.documents[provider._index]) == 1


def test_forget_tool_deletes(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    provider.handle_tool_call("es_memory_remember", {"content": "temporary"})
    doc_id = next(iter(fake_es.documents[provider._index]))

    payload = json.loads(provider.handle_tool_call("es_memory_forget", {"memory_id": doc_id}))

    assert payload["deleted"] is True
    assert fake_es.documents[provider._index] == {}


def test_status_tool_reports_degraded_semantic(tmp_path, configured, fake_es):
    fake_es.inference_available = False
    provider = _provider(tmp_path)

    payload = json.loads(provider.handle_tool_call("es_memory_status", {}))

    assert payload["namespace"] == provider._namespace
    assert payload["retrieval_configured"] == "auto"


def test_unknown_tool_is_an_error(tmp_path, configured, fake_es):
    result = json.loads(_provider(tmp_path).handle_tool_call("nope", {}))
    assert result.get("error")


def test_tool_call_without_a_query_is_an_error(tmp_path, configured, fake_es):
    result = json.loads(_provider(tmp_path).handle_tool_call("es_memory_search", {"query": "  "}))
    assert result.get("error")


# -- shutdown -------------------------------------------------------------


def test_shutdown_closes_the_client(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    provider.shutdown()
    assert provider._client is None
