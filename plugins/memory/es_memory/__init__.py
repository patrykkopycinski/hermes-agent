"""es_memory — Elasticsearch-backed memory plugin (MemoryProvider).

Stores turns and explicit facts as documents in one index and recalls them per turn.
Retrieval is lexical (BM25), semantic (``semantic_text``/ELSER), or ``auto`` — which prefers
semantic and silently degrades to BM25 when the cluster will not run the inference endpoint.

Config: ``$HERMES_HOME/es_memory/config.json`` (written by the dashboard panel / ``hermes memory
setup``), else ``memory.es_memory`` in config.yaml, else the scoped env fallbacks declared in
``config_schema.py``. Secrets (``ES_MEMORY_API_KEY``, ``ES_MEMORY_PASSWORD``) stay in the env store.
Memories are partitioned by ``namespace`` (the profile) so a multiplexed secondary never recalls
the default profile's memories.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt, spawn_context_thread
from agent.secret_scope import get_secret
from hermes_constants import get_hermes_home
from tools.registry import tool_error
from utils import is_truthy_value, read_json_or_empty

from .client import (
    DEFAULT_BOOTSTRAP_TIMEOUT, DEFAULT_INDEX, DEFAULT_INFERENCE_ID, DEFAULT_TIMEOUT, DEFAULT_URL,
    DEFAULT_WRITE_TIMEOUT, SEMANTIC_FIELD, ElasticsearchClient, ElasticsearchError, build_mapping,
    cloud_id_to_url, is_semantic_unavailable,
)
from .tool_schemas import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

# Elastic's brand mark, for the recall indicator.
_GLYPH = "🔎"
_DEFAULT_TOP_K = 5
# Turn text is stored verbatim; cap it so one pathological turn cannot blow the index doc limit.
_MAX_CONTENT_CHARS = 8_000
_VALID_KINDS = ("turn", "fact", "preference", "decision")
# Superseded facts must never come back from recall. Turns carry no ``active`` field, so the
# exclusion is a must_not on false rather than a requirement that it be true.
_EXCLUDE_SUPERSEDED = ({"term": {"active": False}},)
# Remote ES port behind the retired es-memory plugin's SSH preset. Only ever rendered into the
# tunnel hint below — this provider never opens a tunnel itself.
_DEFAULT_SSH_REMOTE_PORT = 9220
_SOURCE_FIELDS = ["content", "kind", "target", "tags", "source", "created_at", "session_id"]


def _load_file_config() -> dict[str, Any]:
    """``$HERMES_HOME/es_memory/config.json`` (profile-scoped, written by the dashboard panel)."""
    return read_json_or_empty(get_hermes_home() / "es_memory" / "config.json")


def _load_yaml_config() -> dict[str, Any]:
    """``memory.es_memory`` from config.yaml (empty on any error)."""
    try:
        from hermes_cli.config import load_config_readonly  # canonical: managed overlay + ${VAR} expansion

        block = (load_config_readonly().get("memory") or {}).get("es_memory")
    except Exception:
        block = None
    return dict(block) if isinstance(block, dict) else {}


def _resolve_config() -> dict[str, Any]:
    """config.json wins over config.yaml; both win over the env fallbacks resolved per field."""
    return {**_load_yaml_config(), **_load_file_config()}


def _setting(config: dict[str, Any], key: str, env_key: str, default: str = "") -> str:
    """One non-secret field: stored config, then the profile-scoped env fallback, then *default*."""
    value = config.get(key)
    if value is None or value == "":
        value = get_secret(env_key, "")
    return str(value).strip() if value not in (None, "") else default


def _int_setting(config: dict[str, Any], key: str, default: int) -> int:
    """Numeric config field, falling back to *default* on a missing or unparseable value."""
    try:
        raw = config.get(key)
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        logger.warning("es_memory: invalid %s=%r; using %s", key, config.get(key), default)
        return default


def _float_setting(config: dict[str, Any], key: str, default: float) -> float:
    try:
        raw = config.get(key)
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        logger.warning("es_memory: invalid %s=%r; using %s", key, config.get(key), default)
        return default


def _profile_namespace(hermes_home: str) -> str:
    """Profile directory name as the isolation key; the default profile is ``default``."""
    name = Path(str(hermes_home or "")).name
    return "default" if name in ("", ".hermes") else name


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any) -> str:
    return str(text or "").strip()[:_MAX_CONTENT_CHARS]


def _tunnel_hint(url: str, ssh_host: str, remote_port: int) -> str:
    """The exact ``ssh -L`` command for a tunnelled endpoint, or "" when none is configured.

    The retired ``es-memory`` plugin opened this tunnel itself with ``subprocess.Popen``. A
    memory provider spawning and owning an ssh process is a lifecycle it cannot win — the child
    outlives a crashed agent, a second profile silently rides the first one's forward, and
    ``initialize()`` blocks on a fixed sleep waiting for it. Hermes can print the command; the
    user (or their ssh config / autossh) owns the process.
    """
    if not ssh_host:
        return ""
    local_port = urlparse(url).port or 9200
    return f"ssh -N -o ExitOnForwardFailure=yes -L {local_port}:127.0.0.1:{remote_port} {ssh_host}"


def _fact_doc_id(namespace: str, target: str, content: str) -> str:
    """Deterministic id for a mirrored memory-tool entry.

    Keyed on the namespace too, because one index serves every profile. Determinism makes a
    retried write idempotent instead of duplicating, and lets a later ``replace``/``remove``
    that names its predecessor via ``old_text`` address the exact entry it supersedes.
    """
    return hashlib.sha256(f"{namespace}:{target}:{_clip(content)}".encode()).hexdigest()[:32]


def _content_doc_id(namespace: str, scope: str, content: str) -> str:
    """Deterministic id for a write whose only identity is its own content.

    EVERY write path needs one, not just the mirrored memory-tool facts. A `semantic_text`
    write runs ELSER inline and can exceed the client timeout while Elasticsearch still stores
    the document, so the caller is told a successful write failed; whoever retries (the model
    re-calling ``es_memory_remember``, most often) would then write a second copy under a fresh
    UUID. A content-addressed id makes that retry an idempotent overwrite instead.

    *scope* keeps genuinely distinct writes distinct: the session id for turns, so the same
    exchange in two sessions stays two documents.
    """
    return hashlib.sha256(f"{namespace}:{scope}:{_clip(content)}".encode()).hexdigest()[:32]


class ESMemoryProvider(MemoryProvider):
    """Elasticsearch long-term memory: one index, BM25 or ``semantic_text`` recall, per-profile scope."""

    def __init__(self, query_rewriter: Callable[[str], str] | None = None) -> None:
        self._config: dict[str, Any] = {}
        self._client: ElasticsearchClient | None = None
        self._query_rewriter = query_rewriter
        self._query_rewrite_enabled = False
        self._index = DEFAULT_INDEX
        self._inference_id = ""       # "" once semantic is ruled out for this index
        self._retrieval = "auto"
        self._top_k = _DEFAULT_TOP_K
        self._namespace = "default"
        self._user_id = "default"
        self._session_id = ""
        self._write_enabled = True    # subagent / cron / flush contexts read but never write
        self._lock = threading.Lock()  # guards the prefetch cache below
        self._pending_recall: list[dict[str, Any]] = []
        self._last_recall_count = 0
        self._prefetch_thread: threading.Thread | None = None
        self._tunnel_hint = ""

    @property
    def name(self) -> str:
        return "es_memory"

    # -- Lifecycle ----------------------------------------------------------

    def is_available(self) -> bool:
        """An endpoint was actually configured. Deliberately offline: the ABC forbids network here.

        No ``DEFAULT_URL`` fallback: that default exists so ``initialize()`` has something to dial,
        but applying it here would make the provider report "ready" on every machine, including
        ones with no cluster at all, and the dashboard would never show it as needing setup.
        """
        config = _resolve_config()
        return bool(_setting(config, "cloud_id", "ES_MEMORY_CLOUD_ID")
                    or _setting(config, "url", "ES_MEMORY_URL"))

    def unavailable_reason(self) -> str:
        base = "No Elasticsearch endpoint configured — set a Cluster URL or Cloud ID (hermes memory setup es_memory)."
        config = _resolve_config()
        hint = _tunnel_hint(_setting(config, "url", "ES_MEMORY_URL", DEFAULT_URL),
                            _setting(config, "ssh_tunnel_host", "ES_MEMORY_SSH_TUNNEL_HOST"),
                            _int_setting(config, "ssh_remote_port", _DEFAULT_SSH_REMOTE_PORT))
        return f"{base} Open the tunnel first: {hint}" if hint else base

    def initialize(self, session_id: str, **kwargs) -> None:
        config = self._config = _resolve_config()
        cloud_id = _setting(config, "cloud_id", "ES_MEMORY_CLOUD_ID")
        url = cloud_id_to_url(cloud_id) if cloud_id else _setting(config, "url", "ES_MEMORY_URL", DEFAULT_URL)
        # Informational only: rendered into errors so a dead tunnel says how to reopen it.
        self._tunnel_hint = _tunnel_hint(
            url, _setting(config, "ssh_tunnel_host", "ES_MEMORY_SSH_TUNNEL_HOST"),
            _int_setting(config, "ssh_remote_port", _DEFAULT_SSH_REMOTE_PORT))
        self._client = ElasticsearchClient(
            url,
            api_key=get_secret("ES_MEMORY_API_KEY", ""),
            username=_setting(config, "username", "ES_MEMORY_USERNAME"),
            password=get_secret("ES_MEMORY_PASSWORD", ""),
            verify_certs=is_truthy_value(config.get("verify_certs", True), default=True),
            timeout=_float_setting(config, "timeout", DEFAULT_TIMEOUT),
            bootstrap_timeout=_float_setting(config, "bootstrap_timeout", DEFAULT_BOOTSTRAP_TIMEOUT),
            write_timeout=_float_setting(config, "write_timeout", DEFAULT_WRITE_TIMEOUT),
        )
        self._index = _setting(config, "index", "ES_MEMORY_INDEX", DEFAULT_INDEX)
        self._retrieval = (str(config.get("retrieval") or "auto")).strip().lower()
        self._inference_id = "" if self._retrieval == "bm25" else (
            _setting(config, "inference_id", "ES_MEMORY_INFERENCE_ID", DEFAULT_INFERENCE_ID))
        self._top_k = max(1, min(int(config.get("top_k") or _DEFAULT_TOP_K), 25))
        self._query_rewrite_enabled = is_truthy_value(config.get("query_rewrite", False))
        # The namespace is the data partition: scope-read it so a multiplexed secondary's memories
        # never land in (or recall from) the default profile's slice of the index.
        self._namespace = (_setting(config, "namespace", "ES_MEMORY_NAMESPACE")
                           or _profile_namespace(str(kwargs.get("hermes_home", ""))))
        self._user_id = str(kwargs.get("user_id") or "default")
        self._session_id = session_id
        self._write_enabled = str(kwargs.get("agent_context") or "primary") == "primary"
        self._bootstrap_index()

    def _bootstrap_index(self) -> None:
        """Create the index if absent, degrading to a BM25-only mapping when semantic is unavailable."""
        if self._client is None or self._client.index_exists(self._index):
            self._reconcile_semantic_field()
            return
        try:
            self._client.create_index(self._index, build_mapping(self._inference_id or None))
            return
        except ElasticsearchError as exc:
            if self._retrieval == "semantic" or not self._inference_id:
                logger.warning("es_memory: could not create index %s: %s", self._index, exc)
                return
            logger.info("es_memory: semantic mapping rejected (%s); creating a BM25-only index", exc)
        self._inference_id = ""
        try:
            self._client.create_index(self._index, build_mapping(None))
        except ElasticsearchError as exc:
            logger.warning("es_memory: could not create index %s: %s", self._index, exc)

    def _reconcile_semantic_field(self) -> None:
        """An index made before semantic was configured has no ``semantic_text`` field; recall must
        not query one that is not mapped, so trust the live mapping over the config."""
        if self._client is None or not self._inference_id:
            return
        try:
            mapping = self._client.request("GET", f"/{self._index}/_mapping")
        except ElasticsearchError as exc:
            logger.debug("es_memory: mapping probe failed: %s", exc)
            return
        properties = ((mapping or {}).get(self._index, {}).get("mappings") or {}).get("properties") or {}
        if SEMANTIC_FIELD not in properties:
            logger.info("es_memory: index %s has no %s field; using BM25", self._index, SEMANTIC_FIELD)
            self._inference_id = ""

    def shutdown(self) -> None:
        thread, self._prefetch_thread = self._prefetch_thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        client, self._client = self._client, None
        if client is not None:
            client.close()

    # -- Storage ------------------------------------------------------------

    def _scope_filter(self, kind: str = "") -> list[dict[str, Any]]:
        """Every read, count and delete is confined to this profile's namespace."""
        clauses: list[dict[str, Any]] = [{"term": {"namespace": self._namespace}}]
        if kind:
            clauses.append({"term": {"kind": kind}})
        return clauses

    def _store(self, content: str, *, kind: str, tags: list[str] | None = None,
               source: str = "", session_id: str = "", refresh: str = "false",
               target: str = "", doc_id: str = "") -> str:
        """Index one memory and return its id. Raises :class:`ElasticsearchError` on failure.

        When the cluster refuses the inference call (unlicensed, undeployed model), the semantic
        copy is dropped for the rest of the process and the write is retried as a plain document:
        losing recall quality is a far better outcome than losing the memory.
        """
        if self._client is None:
            raise ElasticsearchError("es_memory is not initialized")
        doc_id = doc_id or uuid.uuid4().hex
        text = _clip(content)
        document = {
            "content": text,
            "namespace": self._namespace,
            "user_id": self._user_id,
            "session_id": session_id or self._session_id,
            "kind": kind,
            "target": target,
            "active": True,
            "tags": list(tags or []),
            "source": source,
            "created_at": _now(),
        }
        if self._inference_id:
            document[SEMANTIC_FIELD] = text
        try:
            self._client.index_document(self._index, doc_id, document, refresh=refresh)
        except ElasticsearchError as exc:
            if not self._inference_id or self._retrieval == "semantic" or not is_semantic_unavailable(exc):
                raise
            logger.info("es_memory: inference unavailable on write (%s); storing without semantic copy", exc)
            self._inference_id = ""
            self._client.index_document(self._index, doc_id, {k: v for k, v in document.items()
                                                              if k != SEMANTIC_FIELD}, refresh=refresh)
        return doc_id

    def _query_body(self, query: str, size: int, kind: str = "") -> dict[str, Any]:
        """BM25 over ``content``, plus a ``semantic`` clause when the index has the field.

        Two ``should`` clauses rather than an RRF retriever: score fusion by sum needs no
        licensed feature, so the same body works on a basic-tier cluster.
        """
        should: list[dict[str, Any]] = [{"match": {"content": {"query": query}}}]
        if self._inference_id:
            should.append({"semantic": {"field": SEMANTIC_FIELD, "query": query}})
        return {
            "size": size,
            "query": {"bool": {"filter": self._scope_filter(kind), "should": should,
                               "minimum_should_match": 1, "must_not": list(_EXCLUDE_SUPERSEDED)}},
            "_source": _SOURCE_FIELDS,
        }

    @staticmethod
    def _as_memories(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"memory_id": hit.get("_id"), "score": hit.get("_score"), **(hit.get("_source") or {})}
                for hit in hits]

    def _search(self, query: str, *, size: int, kind: str = "") -> list[dict[str, Any]]:
        """Run a recall query, degrading to BM25 once if the semantic clause is rejected."""
        if self._client is None or not query.strip():
            return []
        try:
            return self._as_memories(self._client.search(self._index, self._query_body(query, size, kind)))
        except ElasticsearchError as exc:
            if not self._inference_id or self._retrieval == "semantic" or not is_semantic_unavailable(exc):
                raise
            # The endpoint was reachable at bootstrap but is gone (or never finished deploying):
            # drop semantic for the rest of the process rather than failing every recall.
            logger.info("es_memory: semantic query rejected (%s); falling back to BM25", exc)
            self._inference_id = ""
            return self._as_memories(self._client.search(self._index, self._query_body(query, size, kind)))

    # -- Recall -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        mode = "semantic + BM25" if self._inference_id else "BM25"
        return (f"# Elasticsearch Memory\nActive. Index: {self._index} (namespace: {self._namespace}, {mode}).\n"
                "Use es_memory_search to look up prior context, es_memory_remember to store durable facts, "
                "es_memory_list to review recent memories, and es_memory_forget to delete one.")

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Recall in the background at turn end; :meth:`prefetch` consumes it next turn."""
        if self._client is None or is_trivial_prompt(query):
            return
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            logger.debug("es_memory: prefetch still running; skipping this turn")
            return
        self._prefetch_thread = spawn_context_thread(self._run_prefetch, args=(query,), name="es-memory-prefetch")
        self._prefetch_thread.start()

    def _run_prefetch(self, query: str) -> None:
        if self._query_rewrite_enabled and self._query_rewriter is not None:
            query = self._query_rewriter(query).strip() or query
        try:
            results = self._search(query, size=self._top_k)
        except Exception as exc:
            logger.debug("es_memory prefetch failed: %s", exc)
            return
        with self._lock:
            self._pending_recall = results

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Consume the queued recall as a context block ("" when nothing was recalled)."""
        with self._lock:
            results, self._pending_recall = self._pending_recall, []
            self._last_recall_count = len(results)
        if not results:
            return ""
        lines = [f"- [{item.get('kind') or 'memory'}] {item.get('content', '')}" for item in results]
        return "## Elasticsearch Memory\n" + "\n".join(lines)

    def recall_status(self) -> RecallStatus | None:
        return RecallStatus("Elasticsearch", self._last_recall_count, glyph=_GLYPH) if self._last_recall_count else None

    # -- Writes -------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None:
        """Persist one completed turn as a single document (best-effort, never raises)."""
        if not self._write_enabled or self._client is None or not user_content.strip():
            return
        content = f"User: {_clip(user_content)}\nAssistant: {_clip(assistant_content)}"
        scope = session_id or self._session_id
        try:
            self._store(content, kind="turn", source="turn", session_id=session_id,
                        doc_id=_content_doc_id(self._namespace, scope, content))
        except Exception as exc:
            # WARNING, not debug: by this point the semantic degrade has already had its retry,
            # so anything still failing here is a turn silently missing from memory. Debug-level
            # is how 1,142 dropped writes went unnoticed.
            logger.warning("es_memory: turn not stored (%s)", exc)

    def on_memory_write(self, action: str, target: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Mirror a built-in memory-tool write, superseding whatever the write replaced.

        The built-in tool edits a block of lines, so recall must not keep answering with an
        entry the user just rewrote. ``replace``/``remove`` flip the superseded entry's
        ``active`` to false instead of deleting it: the history stays auditable and a
        ``_delete`` race cannot lose a document a concurrent write still points at.
        """
        if not self._write_enabled or self._client is None:
            return
        old_text = str((metadata or {}).get("old_text") or "").strip()
        try:
            if action in ("replace", "remove"):
                # With old_text we can name the exact entry; without it the caller has given no
                # way to, so the whole target is superseded — conservative, but it never leaves
                # a stale fact active beside the new one.
                self._deactivate_entry(target, old_text) if old_text else self._deactivate_target(target)
            if action == "remove" or not content:
                return
            self._store(content, kind="preference" if target == "user" else "fact",
                        source="memory_tool", target=target, refresh="wait_for",
                        doc_id=_fact_doc_id(self._namespace, target, content))
        except Exception as exc:
            logger.warning("es_memory: memory-tool write not mirrored (%s)", exc)

    def _deactivate_entry(self, target: str, old_text: str) -> None:
        """Supersede the single entry matching *old_text*.

        The deterministic id hits it directly; the content-match fallback covers an entry
        written before this id scheme (or by another writer), which would otherwise stay
        active and keep being recalled.
        """
        if self._client.update_document(self._index, _fact_doc_id(self._namespace, target, old_text),
                                        {"active": False}, refresh="wait_for"):
            return
        logger.debug("es_memory: no exact fact id for %r; superseding by content match", target)
        self._supersede({"bool": {
            "filter": [*self._target_filter(target), {"term": {"active": True}}],
            "must": [{"match": {"content": old_text}}],
        }})

    def _deactivate_target(self, target: str) -> None:
        self._supersede({"bool": {"filter": [*self._target_filter(target), {"term": {"active": True}}]}})

    def _target_filter(self, target: str) -> list[dict[str, Any]]:
        """Mirrored entries for one built-in memory block, inside this profile only.

        ``target`` is the scope key, so an agent-authored ``es_memory_remember`` fact (which
        carries ``target: ""``) is never swept up by a built-in memory-tool rewrite.
        """
        return [{"term": {"namespace": self._namespace}}, {"term": {"target": target}}]

    def _supersede(self, query: dict[str, Any]) -> None:
        try:
            self._client.update_by_query(self._index, query, {"source": "ctx._source.active = false"})
        except ElasticsearchError as exc:
            logger.warning("es_memory: fact supersede failed: %s", exc)

    # -- Tools --------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [dict(schema) for schema in TOOL_SCHEMAS]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        handler = _TOOL_HANDLERS.get(tool_name)
        if handler is None:
            return tool_error(f"Unknown tool: {tool_name}")
        if self._client is None:
            return tool_error("es_memory is not initialized")
        try:
            return json.dumps(handler(self, args), default=str)
        except ElasticsearchError as exc:
            # A tunnelled endpoint that stopped answering is almost always a dropped forward,
            # so say how to reopen it rather than making the user rediscover the command.
            return tool_error(f"{exc}. Open the tunnel first: {self._tunnel_hint}"
                              if self._tunnel_hint else str(exc))
        except (TypeError, ValueError) as exc:
            return tool_error(f"Invalid argument: {exc}")

    def _tool_remember(self, args: dict[str, Any]) -> dict[str, Any]:
        content = str(args.get("content") or "").strip()
        if not content:
            return {"error": "content is required"}
        kind = str(args.get("kind") or "fact")
        if kind not in _VALID_KINDS:
            return {"error": f"kind must be one of {', '.join(_VALID_KINDS)}"}
        tags = [str(t) for t in (args.get("tags") or [])]
        # refresh=wait_for so an immediate es_memory_search in the same turn sees the write.
        # Content-addressed id: if the model retries this call (because a slow ELSER write timed
        # out after Elasticsearch had already stored it), the retry overwrites rather than
        # creating a second copy of the same memory.
        memory_id = self._store(content, kind=kind, tags=tags, source="tool", refresh="wait_for",
                                doc_id=_content_doc_id(self._namespace, kind, content))
        return {"memory_id": memory_id, "status": "stored", "kind": kind}

    def _tool_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        size = max(1, min(int(args.get("top_k") or self._top_k), 25))
        results = self._search(query, size=size, kind=str(args.get("kind") or ""))
        return {"results": results, "count": len(results),
                "retrieval": "semantic+bm25" if self._inference_id else "bm25"}

    def _tool_list(self, args: dict[str, Any]) -> dict[str, Any]:
        size = max(1, min(int(args.get("limit") or 20), 100))
        hits = self._client.search(self._index, {
            "size": size,
            "query": {"bool": {"filter": self._scope_filter(str(args.get("kind") or "")),
                               "must_not": list(_EXCLUDE_SUPERSEDED)}},
            "sort": [{"created_at": {"order": "desc"}}],
            "_source": _SOURCE_FIELDS,
        })
        memories = self._as_memories(hits)
        return {"memories": memories, "count": len(memories)}

    def _tool_forget(self, args: dict[str, Any]) -> dict[str, Any]:
        memory_id = str(args.get("memory_id") or "").strip()
        if not memory_id:
            return {"error": "memory_id is required"}
        deleted = self._client.delete_document(self._index, memory_id, refresh="wait_for")
        return {"memory_id": memory_id, "deleted": deleted}

    def _tool_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        info = self._client.ping()
        return {
            "cluster": (info or {}).get("cluster_name"),
            "version": ((info or {}).get("version") or {}).get("number"),
            "url": self._client.url,
            "index": self._index,
            "namespace": self._namespace,
            # `retrieval` is what is ACTIVE now. When it disagrees with `retrieval_configured`
            # the cluster refused inference and recall silently got worse, which is exactly the
            # thing an operator needs to see rather than infer from logs.
            "retrieval": "semantic+bm25" if self._inference_id else "bm25",
            "retrieval_configured": self._retrieval,
            "semantic_degraded": bool(self._retrieval != "bm25" and not self._inference_id),
            # Counts what recall can actually return, so it never disagrees with es_memory_search.
            "stored": self._client.count(self._index, {"bool": {"filter": self._scope_filter(),
                                                                "must_not": list(_EXCLUDE_SUPERSEDED)}}),
        }

    # -- Setup --------------------------------------------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Fields for ``hermes memory setup``; the dashboard panel uses ``config_schema.py``."""
        return [
            {"key": "url", "description": "Elasticsearch URL", "default": DEFAULT_URL},
            {"key": "api_key", "description": "Elasticsearch API key", "secret": True,
             "env_var": "ES_MEMORY_API_KEY", "url": "https://www.elastic.co/docs/api/doc/elasticsearch"},
            {"key": "index", "description": "Index name", "default": DEFAULT_INDEX},
            {"key": "retrieval", "description": "Retrieval mode", "default": "auto",
             "choices": ["auto", "semantic", "bm25"]},
            {"key": "inference_id", "description": "Inference endpoint for semantic_text",
             "default": DEFAULT_INFERENCE_ID},
            {"key": "top_k", "description": "Memories recalled per turn", "default": "5", "type": "integer"},
            {"key": "ssh_tunnel_host", "description": "SSH host serving the URL's loopback port "
             "(documentation only — Hermes never opens the tunnel)", "default": ""},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """Merge *values* into ``$HERMES_HOME/es_memory/config.json`` (the panel's flat-JSON store)."""
        from utils import atomic_json_write

        config_path = Path(hermes_home) / "es_memory" / "config.json"
        atomic_json_write(config_path, {**read_json_or_empty(config_path), **values}, mode=0o600)


# tool name -> bound handler; unknown names are rejected in handle_tool_call.
_TOOL_HANDLERS: dict[str, Callable[[ESMemoryProvider, dict[str, Any]], dict[str, Any]]] = {
    "es_memory_remember": ESMemoryProvider._tool_remember,
    "es_memory_search": ESMemoryProvider._tool_search,
    "es_memory_list": ESMemoryProvider._tool_list,
    "es_memory_forget": ESMemoryProvider._tool_forget,
    "es_memory_status": ESMemoryProvider._tool_status,
}


def register(ctx) -> None:
    """Register the Elasticsearch memory provider with the plugin system."""
    from plugins.memory.query_rewrite import rewrite_memory_query

    ctx.register_memory_provider(ESMemoryProvider(query_rewriter=rewrite_memory_query))
