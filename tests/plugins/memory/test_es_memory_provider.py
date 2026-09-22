"""es_memory provider: config resolution, index bootstrap, write/recall roundtrips.

The Elasticsearch client is always a double — these tests never reach a cluster. The
contracts pinned here are the ones a live smoke test would not catch: that bootstrap is
idempotent, that a cluster without an inference endpoint silently degrades to BM25 instead
of 400ing every recall, and that fact supersession is per-entry when the caller names the
entry it is replacing.
"""
from __future__ import annotations

import json

import pytest

import plugins.memory.es_memory as es_memory
from plugins.memory.es_memory import (
    EsMemoryProvider,
    _fact_entry_key,
    _index_name,
    _load_es_memory_config,
)


class FakeIndices:
    """Stands in for ``client.indices``; records create calls and can reject a mapping."""

    def __init__(self, *, existing=(), semantic_supported=True, mapping_has_semantic=True):
        self.existing = set(existing)
        self.semantic_supported = semantic_supported
        self.mapping_has_semantic = mapping_has_semantic
        self.created = []

    def exists(self, *, index):
        return index in self.existing

    def create(self, *, index, mappings):
        if "semantic_body" in mappings["properties"] and not self.semantic_supported:
            raise RuntimeError("Unknown field type semantic_text")
        self.created.append({"index": index, "mappings": mappings})
        self.existing.add(index)

    def get_mapping(self, *, index):
        properties = dict(_bm25_properties())
        if self.mapping_has_semantic:
            properties["semantic_body"] = {"type": "semantic_text"}
        return {index: {"mappings": {"properties": properties}}}


def _bm25_properties():
    return {"@timestamp": {}, "kind": {}, "target": {}, "session_id": {}, "active": {}, "content": {}}


class FakeEs:
    """Minimal stand-in for ``elasticsearch.Elasticsearch``."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.indices = FakeIndices()
        self.indexed = []
        self.updates = []
        self.update_by_queries = []
        self.searches = []
        self.hits = []
        self.closed = False
        self.index_error = None
        self.update_error = None

    # The real client returns a new object with per-request options; the double just
    # hands back itself so `.options(...).indices.create(...)` resolves.
    def options(self, **_kwargs):
        return self

    def index(self, *, index, id, document):
        if self.index_error:
            raise self.index_error
        self.indexed.append({"index": index, "id": id, "document": document})

    def update(self, *, index, id, doc):
        if self.update_error:
            raise self.update_error
        self.updates.append({"index": index, "id": id, "doc": doc})

    def update_by_query(self, *, index, query, script):
        self.update_by_queries.append({"index": index, "query": query, "script": script})

    def search(self, *, index, **body):
        self.searches.append({"index": index, **body})
        return {"hits": {"hits": [{"_source": hit} for hit in self.hits]}}

    def close(self):
        self.closed = True


@pytest.fixture
def fake_es(monkeypatch):
    """Patch ``_EsClient.connect`` to attach a FakeEs, skipping the SDK import entirely."""
    client = FakeEs()

    def _connect(self):
        self._client = client

    monkeypatch.setattr(es_memory._EsClient, "connect", _connect)
    return client


@pytest.fixture
def configured(monkeypatch):
    """Minimal credentials so ``is_available()`` passes."""
    monkeypatch.setenv("ES_MEMORY_URL", "https://es.example:9243")
    monkeypatch.setenv("ES_MEMORY_API_KEY", "test-key")


def _provider(tmp_path, *, session="sess-1", **init_kwargs):
    provider = EsMemoryProvider()
    provider.initialize(session, hermes_home=str(tmp_path), platform="cli", **init_kwargs)
    return provider


# -- config ---------------------------------------------------------------


def test_config_file_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ES_MEMORY_URL", "https://from-env:9200")
    monkeypatch.setenv("ES_MEMORY_INDEX_PREFIX", "env-prefix")
    config_dir = tmp_path / "es_memory"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"url": "https://from-file:9200", "retrieval": "hybrid", "top_k": 3}), encoding="utf-8")

    config = _load_es_memory_config(str(tmp_path))

    assert config["url"] == "https://from-file:9200"
    assert config["retrieval"] == "hybrid"
    assert config["top_k"] == 3
    # A key absent from the file still falls back to the environment.
    assert config["index_prefix"] == "env-prefix"


def test_config_rejects_unknown_strategy_and_bad_top_k(tmp_path):
    config_dir = tmp_path / "es_memory"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"retrieval": "telepathy", "top_k": "not-a-number"}), encoding="utf-8")

    config = _load_es_memory_config(str(tmp_path))

    assert config["retrieval"] == "auto"
    assert config["top_k"] == 8


def test_save_config_merges_into_profile_json(tmp_path):
    EsMemoryProvider().save_config({"index_prefix": "first", "top_k": 4}, str(tmp_path))
    EsMemoryProvider().save_config({"top_k": 9}, str(tmp_path))

    stored = json.loads((tmp_path / "es_memory" / "config.json").read_text(encoding="utf-8"))
    assert stored == {"index_prefix": "first", "top_k": 9}


def test_index_name_isolates_profiles_sharing_an_identity():
    """Two profiles both named 'default' must not share an index — the home hash is the
    only thing standing between them."""
    a = _index_name("hermes-memory", "/home/a/.hermes", "default")
    b = _index_name("hermes-memory", "/home/b/.hermes", "default")

    assert a != b
    assert a.startswith("hermes-memory-default-")


# -- availability ---------------------------------------------------------


def test_unavailable_without_endpoint(monkeypatch):
    monkeypatch.delenv("ES_MEMORY_URL", raising=False)
    monkeypatch.setenv("ES_MEMORY_API_KEY", "test-key")

    provider = EsMemoryProvider()

    assert provider.is_available() is False
    assert "ES_MEMORY_URL" in provider.unavailable_reason()


def test_unavailable_without_credentials(monkeypatch):
    monkeypatch.setenv("ES_MEMORY_URL", "https://es.example:9243")
    monkeypatch.delenv("ES_MEMORY_API_KEY", raising=False)
    monkeypatch.delenv("ES_MEMORY_PASSWORD", raising=False)

    provider = EsMemoryProvider()

    assert provider.is_available() is False
    assert "ES_MEMORY_API_KEY" in provider.unavailable_reason()


def test_available_with_endpoint_and_key(configured):
    assert EsMemoryProvider().is_available() is True


def test_cloud_id_satisfies_the_endpoint_requirement(monkeypatch):
    monkeypatch.delenv("ES_MEMORY_URL", raising=False)
    monkeypatch.setenv("ES_MEMORY_CLOUD_ID", "deployment:abc123")
    monkeypatch.setenv("ES_MEMORY_API_KEY", "test-key")

    assert EsMemoryProvider().is_available() is True


# -- index bootstrap ------------------------------------------------------


def test_bootstrap_creates_semantic_index(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    assert len(fake_es.indices.created) == 1
    created = fake_es.indices.created[0]
    assert created["mappings"]["properties"]["semantic_body"] == {
        "type": "semantic_text", "inference_id": ".elser-2-elasticsearch"}
    assert provider._strategy == "semantic"


def test_bootstrap_is_idempotent_for_an_existing_index(tmp_path, configured, fake_es):
    index = _index_name("hermes-memory", str(tmp_path), "default")
    fake_es.indices.existing.add(index)

    provider = _provider(tmp_path)

    assert fake_es.indices.created == []
    assert provider._strategy == "semantic"


def test_bootstrap_falls_back_to_bm25_when_semantic_text_is_rejected(tmp_path, configured, fake_es):
    """A cluster with no inference endpoint must still get a usable index."""
    fake_es.indices.semantic_supported = False

    provider = _provider(tmp_path)

    assert len(fake_es.indices.created) == 1
    assert "semantic_body" not in fake_es.indices.created[0]["mappings"]["properties"]
    assert provider._strategy == "bm25"


def test_existing_bm25_index_keeps_bm25_strategy(tmp_path, configured, fake_es):
    """An index created by an earlier fallback cannot serve a semantic query; re-reading the
    live mapping is what stops every recall from 400ing."""
    index = _index_name("hermes-memory", str(tmp_path), "default")
    fake_es.indices.existing.add(index)
    fake_es.indices.mapping_has_semantic = False

    assert _provider(tmp_path)._strategy == "bm25"


def test_explicit_bm25_never_requests_a_semantic_mapping(tmp_path, configured, fake_es):
    (tmp_path / "es_memory").mkdir()
    (tmp_path / "es_memory" / "config.json").write_text(
        json.dumps({"retrieval": "bm25"}), encoding="utf-8")

    provider = _provider(tmp_path)

    assert "semantic_body" not in fake_es.indices.created[0]["mappings"]["properties"]
    assert provider._strategy == "bm25"


def test_custom_inference_id_reaches_the_mapping(tmp_path, configured, fake_es, monkeypatch):
    monkeypatch.setenv("ES_MEMORY_INFERENCE_ID", "my-elser")

    _provider(tmp_path)

    assert fake_es.indices.created[0]["mappings"]["properties"]["semantic_body"]["inference_id"] == "my-elser"


# -- episodic writes ------------------------------------------------------


def test_sync_turn_indexes_a_turn_document(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    provider.sync_turn("what is the retention policy?", "30 days.", session_id="sess-9")

    assert len(fake_es.indexed) == 1
    document = fake_es.indexed[0]["document"]
    assert document["kind"] == "turn"
    assert document["session_id"] == "sess-9"
    assert "retention policy" in document["content"]
    # Semantic strategy mirrors content into the inference-backed field.
    assert document["semantic_body"] == document["content"]


def test_sync_turn_omits_semantic_body_under_bm25(tmp_path, configured, fake_es):
    fake_es.indices.semantic_supported = False
    provider = _provider(tmp_path)

    provider.sync_turn("hello", "hi")

    assert "semantic_body" not in fake_es.indexed[0]["document"]


def test_sync_turn_skipped_for_non_primary_contexts(tmp_path, configured, fake_es):
    """Cron and subagent turns are not this profile's conversation history."""
    provider = _provider(tmp_path, agent_context="subagent")

    provider.sync_turn("hello", "hi")

    assert fake_es.indexed == []


def test_empty_turn_is_not_written(tmp_path, configured, fake_es):
    _provider(tmp_path).sync_turn("", "")

    assert fake_es.indexed == []


def test_failed_write_is_queued_and_replayed_at_session_end(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.index_error = RuntimeError("cluster_block_exception")

    provider.sync_turn("question", "answer")
    assert fake_es.indexed == []
    assert len(provider._pending_writes) == 1

    fake_es.index_error = None
    provider.on_session_end([])

    assert len(fake_es.indexed) == 1
    assert provider._pending_writes == []


def test_pending_write_queue_is_bounded(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.index_error = RuntimeError("down")

    for i in range(es_memory._MAX_PENDING_WRITES + 10):
        provider.sync_turn(f"q{i}", "a")

    assert len(provider._pending_writes) == es_memory._MAX_PENDING_WRITES


# -- fact supersession ----------------------------------------------------


def test_memory_add_writes_an_active_fact_with_a_content_derived_id(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    provider.on_memory_write("add", "user", "prefers dark mode")

    (written,) = fake_es.indexed
    assert written["document"]["kind"] == "fact"
    assert written["document"]["target"] == "user"
    assert written["document"]["active"] is True
    assert written["id"] == _fact_entry_key(provider._index, "user", "prefers dark mode")


def test_retried_add_is_idempotent(tmp_path, configured, fake_es):
    """Same content -> same doc id, so a replay overwrites rather than duplicating."""
    provider = _provider(tmp_path)

    provider.on_memory_write("add", "memory", "ships on fridays")
    provider.on_memory_write("add", "memory", "ships on fridays")

    assert fake_es.indexed[0]["id"] == fake_es.indexed[1]["id"]


def test_replace_with_old_text_deactivates_only_that_entry(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    provider.on_memory_write("replace", "user", "prefers light mode",
                             metadata={"old_text": "prefers dark mode"})

    assert fake_es.updates[0]["id"] == _fact_entry_key(provider._index, "user", "prefers dark mode")
    assert fake_es.updates[0]["doc"] == {"active": False}
    # No whole-target sweep: the target's other facts stay active.
    assert fake_es.update_by_queries == []
    assert fake_es.indexed[0]["document"]["content"] == "prefers light mode"


def test_replace_without_old_text_supersedes_the_whole_target(tmp_path, configured, fake_es):
    """The caller gave no way to name one entry, so never leave a stale fact active."""
    provider = _provider(tmp_path)

    provider.on_memory_write("replace", "memory", "new state")

    (sweep,) = fake_es.update_by_queries
    assert {"term": {"target": "memory"}} in sweep["query"]["bool"]["filter"]
    assert sweep["script"] == {"source": "ctx._source.active = false"}


def test_remove_with_old_text_falls_back_to_content_match(tmp_path, configured, fake_es):
    """An entry written before this id scheme has no matching doc id; a content match is
    what stops it from staying active forever."""
    provider = _provider(tmp_path)
    fake_es.update_error = RuntimeError("document_missing_exception")

    provider.on_memory_write("remove", "user", "", metadata={"old_text": "legacy fact"})

    (sweep,) = fake_es.update_by_queries
    assert sweep["query"]["bool"]["must"] == [{"match": {"content": "legacy fact"}}]
    # A remove never writes a replacement document.
    assert fake_es.indexed == []


def test_remove_without_old_text_supersedes_the_target(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    provider.on_memory_write("remove", "user", "")

    assert len(fake_es.update_by_queries) == 1
    assert fake_es.indexed == []


# -- recall ---------------------------------------------------------------


def test_prefetch_returns_formatted_context(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.hits = [
        {"kind": "fact", "content": "prefers dark mode"},
        {"kind": "turn", "content": "we discussed retention"},
    ]

    context = provider.prefetch("dark mode")

    assert context.startswith("<es-memory-context>")
    assert "[fact] prefers dark mode" in context
    assert "[turn] we discussed retention" in context
    status = provider.recall_status()
    assert status is not None and status.count == 2


def test_recalled_entries_are_single_lines_never_cut_mid_word(tmp_path, configured, fake_es):
    """A multi-line turn doc or a raw char slice injects text the model reads as a stray,
    clipped user message. Each hit is one line; an overlong one ends on a whole word plus an
    explicit marker, so its visible text is a word-aligned prefix of the stored content."""
    long_turn = "[user]\nwhat did we decide about retention\n[assistant]\n" + "retention policy " * 40
    fake_es.hits = [{"kind": "turn", "content": long_turn}, {"kind": "fact", "content": "prefers\ndark mode"}]
    provider = _provider(tmp_path)

    entries = provider.prefetch("retention").splitlines()[1:-1]

    assert len(entries) == len(fake_es.hits)
    clipped = entries[0].removeprefix("- [turn] ")
    assert clipped.endswith(" […]")
    visible = clipped.removesuffix(" […]")
    flat_words = " ".join(long_turn.split()).split(" ")
    assert visible.split(" ") == flat_words[:len(visible.split(" "))]
    assert entries[1] == "- [fact] prefers dark mode"


def test_spilled_recall_preview_holds_only_whole_entries(tmp_path, configured, fake_es):
    """The core spill preview slices at newlines; with one entry per line, every line it keeps
    must be a complete entry from the block, never the tail of a longer one."""
    from tools.hook_output_spill import spill_if_oversized

    fake_es.hits = [{"kind": "turn", "content": f"[user]\nq{i}\n[assistant]\n" + f"answer {i} " * 60}
                    for i in range(8)]
    provider = _provider(tmp_path)
    block = provider.prefetch("anything")
    config = {"enabled": True, "max_chars": 500, "preview_head": 700, "preview_tail": 700,
              "directory": str(tmp_path / "spill")}

    preview = spill_if_oversized(block, session_id="s", config=config)

    entries = [line for line in block.splitlines() if line.startswith("- [")]
    fences = {"<es-memory-context>", "</es-memory-context>", "--- head ---", "--- tail ---"}
    kept = [line for line in preview.splitlines()[1:] if line not in fences]
    assert kept and all(line in entries for line in kept)


def test_prefetch_returns_empty_without_hits(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    assert provider.prefetch("nothing stored") == ""
    assert provider.recall_status() is None


def test_semantic_recall_excludes_superseded_facts(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)

    provider.prefetch("anything")

    query = fake_es.searches[0]["query"]["bool"]
    assert query["must"] == [{"semantic": {"field": "semantic_body", "query": "anything"}}]
    assert query["must_not"] == [{"term": {"active": False}}]


def test_bm25_recall_uses_a_lexical_query(tmp_path, configured, fake_es):
    fake_es.indices.semantic_supported = False
    provider = _provider(tmp_path)

    provider.prefetch("anything")

    assert fake_es.searches[0]["query"]["bool"]["must"] == [{"match": {"content": "anything"}}]


def test_hybrid_recall_uses_an_rrf_retriever(tmp_path, configured, fake_es):
    (tmp_path / "es_memory").mkdir()
    (tmp_path / "es_memory" / "config.json").write_text(
        json.dumps({"retrieval": "hybrid"}), encoding="utf-8")
    provider = _provider(tmp_path)

    provider.prefetch("anything")

    legs = fake_es.searches[0]["retriever"]["rrf"]["retrievers"]
    assert len(legs) == 2
    # Each leg filters superseded facts itself — RRF has no shared post-filter.
    for leg in legs:
        assert leg["standard"]["query"]["bool"]["must_not"] == [{"term": {"active": False}}]


def test_recall_honours_top_k(tmp_path, configured, fake_es):
    (tmp_path / "es_memory").mkdir()
    (tmp_path / "es_memory" / "config.json").write_text(json.dumps({"top_k": 3}), encoding="utf-8")
    provider = _provider(tmp_path)

    provider.prefetch("anything")

    assert fake_es.searches[0]["size"] == 3


def test_queue_prefetch_warms_the_cache_consumed_by_prefetch(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.hits = [{"kind": "fact", "content": "cached hit"}]

    provider.queue_prefetch("dark mode", session_id="sess-1")
    provider._prefetch_thread.join(timeout=5)  # join the worker rather than sleeping on a guess

    assert provider._recall_cache["sess-1"][1] == 1
    fake_es.hits = []
    # Served from the cache, so the now-empty cluster does not matter.
    assert "cached hit" in provider.prefetch("dark mode", session_id="sess-1")


# -- tools ----------------------------------------------------------------


def test_tool_schema_is_exposed(tmp_path, configured, fake_es):
    (schema,) = _provider(tmp_path).get_tool_schemas()

    assert schema["name"] == "es_memory_search"
    assert "query" in schema["parameters"]["properties"]


def test_tool_call_returns_results_json(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.hits = [{"kind": "fact", "content": "a fact"}, {"kind": "turn", "content": "a turn"}]

    payload = json.loads(provider.handle_tool_call("es_memory_search", {"query": "anything"}))

    assert payload["count"] == 2
    assert payload["strategy"] == "semantic"
    assert payload["index"] == provider._index


def test_tool_call_filters_by_kind(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.hits = [{"kind": "fact", "content": "a fact"}, {"kind": "turn", "content": "a turn"}]

    payload = json.loads(provider.handle_tool_call("es_memory_search", {"query": "x", "kind": "fact"}))

    assert payload["count"] == 1
    assert payload["results"][0]["kind"] == "fact"


def test_unknown_tool_is_an_error(tmp_path, configured, fake_es):
    result = json.loads(_provider(tmp_path).handle_tool_call("nope", {}))

    assert result.get("error")


def test_tool_call_without_a_query_is_an_error(tmp_path, configured, fake_es):
    result = json.loads(_provider(tmp_path).handle_tool_call("es_memory_search", {"query": "  "}))

    assert result.get("error")


# -- shutdown -------------------------------------------------------------


def test_shutdown_flushes_and_closes(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    fake_es.index_error = RuntimeError("down")
    provider.sync_turn("q", "a")
    fake_es.index_error = None

    provider.shutdown()

    assert len(fake_es.indexed) == 1
    assert fake_es.closed is True
    assert provider._active is False


def test_session_switch_clears_stale_recall(tmp_path, configured, fake_es):
    provider = _provider(tmp_path)
    provider._recall_cache["sess-1"] = ("<es-memory-context>\n- stale\n</es-memory-context>", 1)

    provider.on_session_switch("sess-2", reset=True)

    assert provider._recall_cache == {}
    assert provider._session_id == "sess-2"
