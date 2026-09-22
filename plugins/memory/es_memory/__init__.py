"""Elasticsearch memory provider: episodic turn capture + long-term fact storage on a
real Elastic Stack, recalled through ``semantic_text``/ELSER instead of a bespoke vector
store.

Config: $HERMES_HOME/es_memory/config.json (profile-scoped, written by the dashboard and
by ``hermes memory setup``), else env: ES_MEMORY_URL / ES_MEMORY_CLOUD_ID /
ES_MEMORY_API_KEY / ES_MEMORY_USERNAME / ES_MEMORY_PASSWORD / ES_MEMORY_INDEX_PREFIX /
ES_MEMORY_INFERENCE_ID.

Index layout — one index per profile, ``<prefix>-<identity>-<sha256(hermes_home)[:10]>``,
holding two document kinds:

- ``kind: turn`` — one immutable doc per completed turn (``sync_turn``).
- ``kind: fact`` — mirrors the built-in ``memory add/replace/remove`` tool. ``target`` is
  ``memory`` or ``user``; superseded entries flip ``active: false`` rather than being
  deleted, so recall can exclude them while the history stays auditable.

Retrieval is configurable (``auto`` | ``semantic`` | ``hybrid`` | ``bm25``) because
``semantic_text`` needs an inference endpoint the cluster may not have: ``auto`` probes
once at bootstrap and degrades to BM25 rather than leaving the provider dead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread
from agent.secret_scope import get_secret
from hermes_constants import get_hermes_home
from tools.registry import tool_error
from utils import read_json_or_empty

logger = logging.getLogger(__name__)

_DEFAULT_INDEX_PREFIX = "hermes-memory"
_DEFAULT_INFERENCE_ID = ".elser-2-elasticsearch"
_DEFAULT_TOP_K = 8
_DEFAULT_TIMEOUT = 30.0
# Creating an index with a semantic_text mapping allocates the inference endpoint, measured
# at ~32s on a warm cluster and worse on a cold one. Everything else keeps _DEFAULT_TIMEOUT.
_BOOTSTRAP_TIMEOUT = 120.0
# Writes that fail are replayed at the next session boundary; cap the buffer so a long
# outage costs bounded memory instead of the whole session's turns.
_MAX_PENDING_WRITES = 50
_MAX_RECALL_CHARS = 300
_MAX_QUERY_CHARS = 400
_STRATEGIES = ("auto", "semantic", "hybrid", "bm25")
# Contexts whose turns are not this profile's conversation history.
_NON_WRITING_CONTEXTS = frozenset({"cron", "flush", "subagent"})


def _sanitize(raw: str) -> str:
    """Lowercase a name into the subset ES index names allow: no uppercase, no separators
    beyond ``-``/``_``, and no leading ``-``/``_`` (which ES rejects outright)."""
    collapsed = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_-]", "_", (raw or "").lower()))
    return collapsed.strip("-_") or "default"


def _index_name(prefix: str, hermes_home: str, agent_identity: str) -> str:
    """Per-profile isolation: the identity keeps the name human-readable, the home hash
    guarantees two profiles both named 'default' never share an index."""
    home_hash = hashlib.sha256((hermes_home or "").encode()).hexdigest()[:10]
    return f"{_sanitize(prefix) or _DEFAULT_INDEX_PREFIX}-{_sanitize(agent_identity)}-{home_hash}"


def _fact_entry_key(index: str, target: str, content: str) -> str:
    """Stable id for ONE fact entry: ``(index, target, content)``. Two entries under the same
    target are distinct documents, so a ``replace``/``remove`` carrying ``old_text`` touches
    only the entry it names — matching the built-in memory tool's per-line semantics. Keying
    on content (not arrival order) also makes a retried write idempotent."""
    return hashlib.sha256(f"{index}:{target}:{content}".encode()).hexdigest()[:24]


def _one_line(text: str, limit: int) -> str:
    """Collapse whitespace to one line; past *limit*, cut at a word boundary and say so."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    window = flat[:limit + 1]
    return (window[:window.rfind(" ")] if " " in window else flat[:limit]) + " […]"


def _int_setting(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _load_es_memory_config(hermes_home: Optional[str] = None) -> dict:
    """$HERMES_HOME/es_memory/config.json (profile-scoped) overlaid on env defaults.

    Connection identity (endpoint, credentials, index prefix) goes through ``get_secret`` so a
    multiplexed secondary profile never inherits the default profile's cluster or index.
    """
    home = Path(hermes_home) if hermes_home else get_hermes_home()
    config = {
        "url": get_secret("ES_MEMORY_URL", "") or "",
        "cloud_id": get_secret("ES_MEMORY_CLOUD_ID", "") or "",
        "api_key": get_secret("ES_MEMORY_API_KEY", "") or "",
        "username": get_secret("ES_MEMORY_USERNAME", "") or "elastic",
        "password": get_secret("ES_MEMORY_PASSWORD", "") or "",
        "index_prefix": get_secret("ES_MEMORY_INDEX_PREFIX", "") or _DEFAULT_INDEX_PREFIX,
        "inference_id": get_secret("ES_MEMORY_INFERENCE_ID", "") or _DEFAULT_INFERENCE_ID,
        "retrieval": "auto",
        "top_k": _DEFAULT_TOP_K,
    }
    # A corrupt or empty file falls through to the env defaults rather than killing the provider.
    stored = read_json_or_empty(home / "es_memory" / "config.json")
    config.update({k: v for k, v in stored.items() if v not in (None, "")})
    if config["retrieval"] not in _STRATEGIES:
        config["retrieval"] = "auto"
    config["top_k"] = max(1, _int_setting(config["top_k"], _DEFAULT_TOP_K))
    return config


def _semantic_mappings(inference_id: str) -> dict:
    """Mapping with the ELSER-backed ``semantic_text`` field used for semantic recall."""
    mappings = _bm25_mappings()
    mappings["properties"]["semantic_body"] = {"type": "semantic_text", "inference_id": inference_id}
    return mappings


def _bm25_mappings() -> dict:
    """Lexical-only mapping: valid on any cluster, no inference endpoint required."""
    return {
        "properties": {
            "@timestamp": {"type": "date"},
            "kind": {"type": "keyword"},            # turn | fact
            "target": {"type": "keyword"},          # memory | user (facts only)
            "session_id": {"type": "keyword"},
            "active": {"type": "boolean"},          # facts only: false once superseded/removed
            "content": {"type": "text"},
        },
    }


class _EsClient:
    """Thin wrapper over the official ``elasticsearch`` client.

    The SDK import is deferred to :meth:`connect` (not module import) for two reasons the
    other optional-backend providers share: ``elasticsearch`` is an optional extra that
    lazy-installs on first use, and ``is_available()`` runs on every dashboard render, where
    importing a heavy SDK — or failing to — must not matter.
    """

    def __init__(self, config: dict, *, timeout: float = _DEFAULT_TIMEOUT):
        self._config = config
        self._timeout = timeout
        self._client = None

    def connect(self) -> None:
        """Build the underlying client. Raises on a missing SDK or unusable config."""
        from tools import lazy_deps

        lazy_deps.ensure("memory.es_memory", prompt=False)
        from elasticsearch import Elasticsearch

        kwargs: Dict[str, Any] = {"request_timeout": self._timeout}
        if self._config.get("cloud_id") and not self._config.get("url"):
            kwargs["cloud_id"] = self._config["cloud_id"]
        else:
            kwargs["hosts"] = [self._config["url"]]
        if self._config.get("api_key"):
            kwargs["api_key"] = self._config["api_key"]
        elif self._config.get("password"):
            kwargs["basic_auth"] = (self._config.get("username") or "elastic", self._config["password"])
        self._client = Elasticsearch(**kwargs)

    @property
    def raw(self):
        return self._client

    def ensure_index(self, index: str, inference_id: str, *, want_semantic: bool) -> bool:
        """Create *index* if missing; return True when it carries a ``semantic_text`` field.

        Idempotent: an existing index is inspected, never re-created. When the cluster
        rejects the semantic mapping (no inference endpoint, unsupported version) we fall
        back to the lexical mapping instead of leaving the provider with no index at all.
        """
        if self._client.indices.exists(index=index):
            return self._index_has_semantic(index)
        if want_semantic:
            try:
                self._client.options(request_timeout=_BOOTSTRAP_TIMEOUT).indices.create(
                    index=index, mappings=_semantic_mappings(inference_id))
                return True
            except Exception as exc:
                logger.warning("es_memory: semantic_text mapping rejected for %s (%s); falling back to BM25",
                               index, exc)
        self._client.indices.create(index=index, mappings=_bm25_mappings())
        return False

    def _index_has_semantic(self, index: str) -> bool:
        """True when the live mapping already has ``semantic_body``. An index created by an
        earlier BM25 fallback must keep using BM25, or every recall 400s."""
        try:
            mapping = self._client.indices.get_mapping(index=index)
            for entry in dict(mapping).values():
                properties = (entry or {}).get("mappings", {}).get("properties", {})
                if "semantic_body" in properties:
                    return True
        except Exception as exc:
            logger.debug("es_memory: could not read mapping for %s: %s", index, exc)
        return False

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:
                logger.debug("es_memory: client close failed: %s", exc)
            self._client = None


class EsMemoryProvider(MemoryProvider):
    """Elasticsearch-backed episodic + semantic memory."""

    def __init__(self):
        self._client: Optional[_EsClient] = None
        self._config: Dict[str, Any] = {}
        self._index = ""
        self._session_id = ""
        self._hermes_home = ""
        self._strategy = "bm25"
        self._write_enabled = True
        self._active = False
        self._unavailable_reason = ""
        self._pending_writes: List[Dict[str, Any]] = []
        self._recall_cache: Dict[str, tuple[str, int]] = {}
        self._last_recall: Optional[RecallStatus] = None
        # Kept so tests (and shutdown diagnostics) can join the in-flight recall worker.
        self._prefetch_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "es_memory"

    # -- availability -----------------------------------------------------

    def is_available(self) -> bool:
        """Config only, no network and no SDK import (the ABC forbids both here, and this runs
        on every dashboard render). The SDK being absent is NOT unavailability: it lazy-installs
        in ``initialize()``, and returning False here would mean it never gets the chance."""
        try:
            config = _load_es_memory_config()
        except Exception as exc:
            self._unavailable_reason = f"Could not read es_memory config: {exc}"
            return False
        if not (config.get("url") or config.get("cloud_id")):
            self._unavailable_reason = (
                "Set an Elasticsearch endpoint: ES_MEMORY_URL (or ES_MEMORY_CLOUD_ID for Elastic Cloud).")
            return False
        if not (config.get("api_key") or config.get("password")):
            self._unavailable_reason = (
                "Set ES_MEMORY_API_KEY (preferred) or ES_MEMORY_PASSWORD to authenticate against "
                f"{config.get('url') or 'the configured Cloud ID'}.")
            return False
        self._unavailable_reason = ""
        return True

    def unavailable_reason(self) -> str:
        return self._unavailable_reason or "Elasticsearch memory is not configured (no endpoint or credentials)."

    # -- lifecycle --------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Connect and bootstrap the profile's index. Every failure mode leaves the provider
        inert (``_active`` False) rather than raising: a memory backend must never take the
        agent down with it."""
        self._hermes_home = kwargs.get("hermes_home") or str(get_hermes_home())
        self._session_id = session_id
        self._write_enabled = kwargs.get("agent_context", "primary") not in _NON_WRITING_CONTEXTS
        try:
            self._config = _load_es_memory_config(self._hermes_home)
            self._index = _index_name(self._config["index_prefix"], self._hermes_home,
                                      kwargs.get("agent_identity", "default"))
            client = _EsClient(self._config)
            client.connect()
            requested = self._config["retrieval"]
            has_semantic = client.ensure_index(self._index, self._config["inference_id"],
                                               want_semantic=requested != "bm25")
            self._strategy = self._resolve_strategy(requested, has_semantic)
        except Exception as exc:
            logger.warning("es_memory initialization failed: %s", exc, exc_info=True)
            self._client = None
            self._active = False
            return
        self._client = client
        self._active = True
        logger.debug("es_memory ready: index=%s strategy=%s", self._index, self._strategy)

    @staticmethod
    def _resolve_strategy(requested: str, has_semantic: bool) -> str:
        """Collapse ``auto`` and demote semantic/hybrid when the index has no semantic field —
        asking for a strategy the mapping cannot serve would 400 on every single recall."""
        if requested == "bm25":
            return "bm25"
        if not has_semantic:
            return "bm25"
        return "semantic" if requested == "auto" else requested

    def _can_write(self) -> bool:
        return bool(self._active and self._write_enabled and self._client)

    def shutdown(self) -> None:
        self._flush_pending()
        if self._client is not None:
            self._client.close()
            self._client = None
        self._active = False

    # -- episodic turns ---------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None:
        if not self._can_write() or not (user_content or assistant_content):
            return
        self._write("turn", {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "kind": "turn",
            "session_id": session_id or self._session_id,
            "content": f"[user]\n{user_content}\n[assistant]\n{assistant_content}",
        }, doc_id=str(uuid.uuid4()))

    def _write(self, mode: str, doc: dict, *, doc_id: str) -> None:
        """Index one document, queueing it for replay if the cluster rejects the write."""
        body = dict(doc)
        if self._strategy != "bm25":
            body["semantic_body"] = body.get("content", "")
        try:
            self._client.raw.index(index=self._index, id=doc_id, document=body)
        except Exception as exc:
            with self._lock:
                self._pending_writes = (self._pending_writes + [{"doc_id": doc_id, "doc": doc}])[-_MAX_PENDING_WRITES:]
            logger.warning("es_memory %s write failed (%s), queued for retry: %s", mode, doc_id, exc)

    def _flush_pending(self) -> None:
        if not self._can_write():
            return
        with self._lock:
            pending, self._pending_writes = self._pending_writes, []
        for entry in pending:
            self._write("retry", entry["doc"], doc_id=entry["doc_id"])

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        self._flush_pending()

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False,
                          rewound: bool = False, **kwargs) -> None:
        self._flush_pending()
        self._session_id = new_session_id or self._session_id
        self._recall_cache.clear()

    # -- long-term facts (mirrors the built-in memory tool) ----------------

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        if not self._can_write():
            return
        old_text = str((metadata or {}).get("old_text") or "").strip()
        now = datetime.now(timezone.utc).isoformat()

        if action == "remove":
            # With old_text, deactivate exactly that entry; without it the caller has given no
            # way to name one, so fall back to the whole target.
            self._deactivate_entry(target, old_text) if old_text else self._deactivate_target(target)
            return
        if action == "replace":
            # A replace that names its predecessor supersedes only that entry. One that does not
            # falls back to whole-target supersede: conservative, but never leaves a stale fact
            # active next to the new one.
            self._deactivate_entry(target, old_text) if old_text else self._deactivate_target(target)

        self._write("fact", {
            "@timestamp": now, "kind": "fact", "target": target, "active": True, "content": content,
        }, doc_id=_fact_entry_key(self._index, target, content))

    def _deactivate_entry(self, target: str, old_text: str) -> None:
        """Mark the single entry matching *old_text* inactive. Prefers the exact doc id; falls
        back to a content match so an entry written by another writer (or before this id scheme)
        still gets superseded rather than silently left active."""
        try:
            self._client.raw.update(index=self._index, id=_fact_entry_key(self._index, target, old_text),
                                    doc={"active": False})
            return
        except Exception as exc:
            logger.debug("es_memory: exact fact id miss for %s (%s); falling back to content match", target, exc)
        self._update_by_query({"bool": {
            "filter": [{"term": {"target": target}}, {"term": {"kind": "fact"}}, {"term": {"active": True}}],
            "must": [{"match": {"content": old_text}}],
        }})

    def _deactivate_target(self, target: str) -> None:
        self._update_by_query({"bool": {
            "filter": [{"term": {"target": target}}, {"term": {"kind": "fact"}}],
        }})

    def _update_by_query(self, query: dict) -> None:
        try:
            self._client.raw.update_by_query(
                index=self._index, query=query, script={"source": "ctx._source.active = false"})
        except Exception as exc:
            logger.warning("es_memory: fact supersede failed: %s", exc)

    # -- recall -----------------------------------------------------------

    def _search_body(self, query: str, size: int) -> Dict[str, Any]:
        """Search kwargs for the resolved strategy. ``active: false`` docs are superseded facts
        and must never come back; turns carry no ``active`` field, so exclude by term rather
        than requiring ``active: true``."""
        trimmed = query[:_MAX_QUERY_CHARS]
        exclude_superseded = [{"term": {"active": False}}]
        semantic = {"semantic": {"field": "semantic_body", "query": trimmed}}
        lexical = {"match": {"content": trimmed}}
        source = ["kind", "target", "content", "@timestamp"]

        if self._strategy == "hybrid":
            # RRF fuses the two rankings server-side; each leg filters superseded facts itself
            # because the fusion retriever has no shared post-filter.
            return {"size": size, "_source": source, "retriever": {"rrf": {"retrievers": [
                {"standard": {"query": {"bool": {"must": [leg], "must_not": exclude_superseded}}}}
                for leg in (semantic, lexical)
            ]}}}
        primary = semantic if self._strategy == "semantic" else lexical
        return {"size": size, "_source": source,
                "query": {"bool": {"must": [primary], "must_not": exclude_superseded}}}

    def _search(self, query: str, size: int) -> List[Dict[str, Any]]:
        """Run a recall query; [] on any failure — recall degrading to nothing is always
        preferable to a memory backend raising into the turn."""
        if not self._active or not self._client or not query.strip():
            return []
        try:
            result = self._client.raw.search(index=self._index, **self._search_body(query, size))
            return [hit.get("_source", {}) for hit in dict(result).get("hits", {}).get("hits", [])]
        except Exception as exc:
            logger.debug("es_memory search failed: %s", exc, exc_info=True)
            return []

    @staticmethod
    def _format_context(hits: List[Dict[str, Any]]) -> str:
        """One line per hit, never cut mid-word. A multi-line turn doc (``[user]``/``[assistant]``
        on their own lines) or a raw char slice injects what reads as a stray, clipped user message;
        one-entry-per-line also keeps the spill preview (which slices at newlines) on entry bounds."""
        if not hits:
            return ""
        lines = [f"- [{hit.get('kind', '?')}] {_one_line(str(hit.get('content', '')), _MAX_RECALL_CHARS)}"
                 for hit in hits]
        return "<es-memory-context>\n" + "\n".join(lines) + "\n</es-memory-context>"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Warm the cache off the turn's critical path, under the spawner's contextvars so the
        worker resolves the same profile home and secret scope."""
        if not self._active or not query.strip():
            return
        self._prefetch_thread = spawn_context_thread(
            self._background_recall, name="es-memory-prefetch",
            args=(query, session_id or self._session_id))
        self._prefetch_thread.start()

    def _recall(self, query: str) -> tuple[str, int]:
        """``(formatted context, hit count)``. The count travels with the text rather than being
        re-derived from it: the indicator must report what was injected, not a parse of it."""
        hits = self._search(query, self._config.get("top_k", _DEFAULT_TOP_K))
        return self._format_context(hits), len(hits)

    def _background_recall(self, query: str, session_id: str) -> None:
        recalled = self._recall(query)
        with self._lock:
            self._recall_cache[session_id] = recalled

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Cached recall from :meth:`queue_prefetch`; falls back to a direct search on the first
        turn of a session, where nothing has been queued yet."""
        if not self._active:
            return ""
        key = session_id or self._session_id
        with self._lock:
            recalled = self._recall_cache.pop(key, None)
        context, count = recalled if recalled is not None else self._recall(query)
        self._last_recall = RecallStatus(provider_label="Elasticsearch", count=count) if context else None
        return context

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    # -- tools ------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [{
            "name": "es_memory_search",
            "description": "Search this profile's Elasticsearch memory index (past turns and stored facts).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "kind": {"type": "string", "enum": ["turn", "fact", "any"],
                             "description": "Restrict to episodic turns or long-term facts. Default: any."},
                },
                "required": ["query"],
            },
        }]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "es_memory_search":
            return tool_error(f"Unknown tool: {tool_name}")
        if not self._active or not self._client:
            return tool_error(self.unavailable_reason())
        query = str(args.get("query") or "").strip()
        if not query:
            return tool_error("query is required")
        kind = str(args.get("kind") or "any")
        hits = self._search(query, max(self._config.get("top_k", _DEFAULT_TOP_K), 10))
        if kind in ("turn", "fact"):
            hits = [hit for hit in hits if hit.get("kind") == kind]
        return json.dumps({"results": hits, "count": len(hits), "index": self._index,
                           "strategy": self._strategy})

    # -- setup surface ----------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Fields for ``hermes memory setup``. Mirrors ``config_schema.CONFIG_SCHEMA``, which the
        dashboard renders instead; the two must stay in sync."""
        return [
            {"key": "url", "description": "Elasticsearch endpoint URL (blank if using a Cloud ID)",
             "required": False, "env_var": "ES_MEMORY_URL"},
            {"key": "cloud_id", "description": "Elastic Cloud deployment ID (used when no URL is set)",
             "required": False, "env_var": "ES_MEMORY_CLOUD_ID"},
            {"key": "api_key", "description": "Elasticsearch API key (preferred over basic auth)",
             "secret": True, "required": False, "env_var": "ES_MEMORY_API_KEY"},
            {"key": "username", "description": "Basic-auth username (ignored when an API key is set)",
             "required": False, "default": "elastic", "env_var": "ES_MEMORY_USERNAME"},
            {"key": "password", "description": "Basic-auth password (ignored when an API key is set)",
             "secret": True, "required": False, "env_var": "ES_MEMORY_PASSWORD"},
            {"key": "retrieval", "description": "Retrieval strategy", "required": False, "default": "auto",
             "choices": list(_STRATEGIES)},
            {"key": "inference_id", "description": "Inference endpoint backing semantic_text",
             "required": False, "default": _DEFAULT_INFERENCE_ID, "env_var": "ES_MEMORY_INFERENCE_ID"},
            {"key": "index_prefix", "description": "Index name prefix", "required": False,
             "default": _DEFAULT_INDEX_PREFIX, "env_var": "ES_MEMORY_INDEX_PREFIX"},
            {"key": "top_k", "description": "How many documents recall injects", "required": False,
             "default": _DEFAULT_TOP_K, "type": "integer", "minimum": 1, "maximum": 50},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Merge non-secret *values* into $HERMES_HOME/es_memory/config.json — the same file the
        dashboard's declared flat-JSON surface writes, so both paths agree."""
        from utils import atomic_json_write

        config_path = Path(hermes_home) / "es_memory" / "config.json"
        atomic_json_write(config_path, {**read_json_or_empty(config_path), **values}, mode=0o600)


def register(ctx):
    ctx.register_memory_provider(EsMemoryProvider())
