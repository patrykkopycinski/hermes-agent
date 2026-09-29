"""Thin Elasticsearch REST layer for the ``es_memory`` provider.

Speaks the ES HTTP API over ``httpx`` (a core Hermes dependency) rather than the official
``elasticsearch`` client, so activating the provider adds no install step. Only the six calls
the provider needs are implemented: ping, index bootstrap, index, search, get-by-id, delete.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://localhost:9200"
DEFAULT_INDEX = "hermes-memory"
# Shipped with Elasticsearch 8.16+; a `semantic_text` field pointing at it deploys ELSER v2
# on first use. Absent (or ML disabled) the bootstrap falls back to a BM25-only mapping.
DEFAULT_INFERENCE_ID = ".elser-2-elasticsearch"
DEFAULT_TIMEOUT = 15.0
# Creating an index whose mapping declares a `semantic_text` field makes Elasticsearch allocate
# the inference endpoint inline; that measured ~32s on a real deployment, so the one-off
# bootstrap call gets its own budget. Sharing DEFAULT_TIMEOUT would time out the create and
# leave the provider with no index on exactly the licensed clusters semantic recall needs.
DEFAULT_BOOTSTRAP_TIMEOUT = 90.0
# Writes are slower than reads by a different order of magnitude when `semantic_text` is on:
# every indexed document runs ELSER inference inline, which is CPU-bound. Measured on a real
# node, one sequential write took ~8.5s, and at 5 concurrent writes 1,028/1,142 exceeded a 15s
# budget — while Elasticsearch had in fact stored the document, so the caller saw a failure for
# a write that succeeded. 5-way concurrency queues the inference (~5 x 8.5s = ~43s), so 60s
# covers the measured worst case with ~40% headroom, plus the up-to-1s `refresh=wait_for` wait.
# Reads keep the short budget: a slow search should fail fast and degrade, not stall a turn.
DEFAULT_WRITE_TIMEOUT = 60.0

SEMANTIC_FIELD = "content_semantic"

# Elasticsearch accepts a `semantic_text` mapping on any licence but rejects the inference call
# itself at index time (403 "current license is non-compliant for [inference]") or when the model
# is not deployed. Existence of the inference endpoint therefore proves nothing — GET
# /_inference/<id> answers happily on a basic cluster — so the write path classifies the failure
# and drops the semantic copy instead of pre-flighting it.
# Phrases that appear only when the failure is about *running* an inference endpoint. Matched
# against the union of the error's `type`, `reason` and every `root_cause[*].type`/`reason` —
# never `reason` alone: an endpoint that is registered but whose deployment is not running
# answers `type: status_exception` with `reason: "Exception when running inference id [x] on
# field [y]"`, and a reason-only match let that through as a hard failure, silently dropping
# every write instead of degrading to BM25.
_SEMANTIC_MARKERS = (
    "non-compliant for [inference]",   # licence tier forbids inference at all
    "inference_not_found",
    "inference id",                    # "Exception when running inference id [...]"
    "inference endpoint",
    "[inference]",
    "model_deployment_not_allocated",
    "trained model",
    "deployment",                      # "... model deployment ... is not started/allocated"
    "semantic_text",
    "semantic query",
)
# Statuses that mean "not right now". A 400 is excluded on purpose: a malformed semantic query
# is OUR bug, and degrading to BM25 would hide it behind quietly worse recall.
_SEMANTIC_UNAVAILABLE_STATUSES = frozenset({403, 404, 408, 409, 429, 500, 502, 503, 504})


def _error_strings(error: Exception) -> list[str]:
    """Every string worth classifying on: the message plus the structured error's type/reason
    and each ``root_cause`` entry's type/reason."""
    parts = [str(error)]
    structured = getattr(error, "error", None)
    if isinstance(structured, dict):
        parts += [str(structured.get("type") or ""), str(structured.get("reason") or "")]
        root = structured.get("root_cause")
        if isinstance(root, list):
            for cause in root:
                if isinstance(cause, dict):
                    parts += [str(cause.get("type") or ""), str(cause.get("reason") or "")]
        caused_by = structured.get("caused_by")
        if isinstance(caused_by, dict):
            parts += [str(caused_by.get("type") or ""), str(caused_by.get("reason") or "")]
    return [p.lower() for p in parts if p]


def is_semantic_unavailable(error: Exception) -> bool:
    """True when *error* means "this cluster cannot run the inference endpoint right now"."""
    status = getattr(error, "status", None)
    if status is not None and status not in _SEMANTIC_UNAVAILABLE_STATUSES:
        return False
    haystack = " ".join(_error_strings(error))
    return any(marker in haystack for marker in _SEMANTIC_MARKERS)


# Non-semantic mapping: everything the provider filters, sorts or displays on.
_BASE_PROPERTIES: dict[str, Any] = {
    "content": {"type": "text"},
    "namespace": {"type": "keyword"},
    "user_id": {"type": "keyword"},
    "session_id": {"type": "keyword"},
    "kind": {"type": "keyword"},
    # Which built-in memory block a fact mirrors ("memory" | "user"); "" for agent-authored ones.
    "target": {"type": "keyword"},
    # Flipped to false when a later memory-tool write supersedes this entry. Recall excludes
    # `active: false` rather than requiring true, so documents predating this field still match.
    "active": {"type": "boolean"},
    "tags": {"type": "keyword"},
    "source": {"type": "keyword"},
    "created_at": {"type": "date"},
}


class ElasticsearchError(RuntimeError):
    """A failed call, carrying the server's own status code and structured ``error`` object.

    The structure matters: Elasticsearch puts the machine-readable discriminator in ``type``
    and ``root_cause[*].type``, not in ``reason``. Classifying on the message alone missed an
    undeployed inference endpoint entirely (see :func:`is_semantic_unavailable`).
    """

    def __init__(self, message: str, *, status: int | None = None, error: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.error = error if isinstance(error, dict) else {}


def build_mapping(inference_id: str | None) -> dict[str, Any]:
    """Index mapping; with *inference_id* it also declares the ``semantic_text`` field.

    ``content`` is never ``copy_to``-ed into it. The provider writes the semantic copy as an
    explicit field, so dropping semantic mid-flight is a change to the document it sends rather
    than an index migration — BM25 keeps working on every document either way.
    """
    properties = dict(_BASE_PROPERTIES)
    if inference_id:
        properties[SEMANTIC_FIELD] = {"type": "semantic_text", "inference_id": inference_id}
    return {"mappings": {"properties": properties}}


class ElasticsearchClient:
    """Minimal synchronous ES client. One ``httpx.Client`` per provider instance."""

    def __init__(
        self,
        url: str,
        *,
        api_key: str = "",
        username: str = "",
        password: str = "",
        verify_certs: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        bootstrap_timeout: float = DEFAULT_BOOTSTRAP_TIMEOUT,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
    ) -> None:
        self.url = re.sub(r"/+$", "", url or DEFAULT_URL)
        self.bootstrap_timeout = bootstrap_timeout
        self.write_timeout = write_timeout
        self._client = httpx.Client(
            base_url=self.url,
            headers=_auth_headers(api_key, username, password),
            verify=verify_certs,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None,
                timeout: float | None = None) -> Any:
        """One JSON round-trip; raises :class:`ElasticsearchError` on a non-2xx response.

        *timeout* overrides the client default for this call only (index bootstrap needs it).
        """
        kwargs: dict[str, Any] = {"json": json_body, "params": params}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # A refused connection or a read timeout is the normal state of a cluster that is
            # down (or a tunnel that is not up). Callers already degrade on ElasticsearchError;
            # letting a raw httpx exception escape would fail the turn instead.
            raise ElasticsearchError(f"{method} {path} -> {type(exc).__name__}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if response.is_success:
            return payload
        raise ElasticsearchError(
            f"{method} {path} -> {response.status_code}: {_error_text(payload)}",
            status=response.status_code,
            error=payload.get("error") if isinstance(payload, dict) else None,
        )

    # -- Operations the provider uses ---------------------------------------

    def ping(self) -> dict[str, Any]:
        """Cluster root document (name, version, ...)."""
        return self.request("GET", "/")

    def index_exists(self, index: str) -> bool:
        return self._client.request("HEAD", f"/{index}").status_code == 200

    def create_index(self, index: str, body: dict[str, Any]) -> None:
        """Create *index*; an existing index is not an error (concurrent Hermes processes race here)."""
        try:
            self.request("PUT", f"/{index}", json_body=body, timeout=self.bootstrap_timeout)
        except ElasticsearchError as exc:
            if "resource_already_exists_exception" not in str(exc):
                raise

    def index_document(self, index: str, doc_id: str, document: dict[str, Any], *, refresh: str = "false") -> Any:
        return self.request("PUT", f"/{index}/_doc/{doc_id}", json_body=document,
                            params={"refresh": refresh}, timeout=self.write_timeout)

    def search(self, index: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Run *body* against *index* and return the raw hit list (``[]`` on a missing index)."""
        try:
            payload = self.request("POST", f"/{index}/_search", json_body=body)
        except ElasticsearchError as exc:
            if "index_not_found_exception" in str(exc):
                return []
            raise
        return list(((payload or {}).get("hits") or {}).get("hits") or [])

    def update_document(self, index: str, doc_id: str, doc: dict[str, Any], *, refresh: str = "false") -> bool:
        """Partial-update one document; False when no such document exists."""
        try:
            self.request("POST", f"/{index}/_update/{doc_id}", json_body={"doc": doc},
                         params={"refresh": refresh}, timeout=self.write_timeout)
        except ElasticsearchError as exc:
            if "document_missing_exception" in str(exc) or "404" in str(exc):
                return False
            raise
        return True

    def update_by_query(self, index: str, query: dict[str, Any], script: dict[str, Any],
                        *, refresh: str = "true") -> int:
        """Apply *script* to every document matching *query*; returns the updated count."""
        try:
            payload = self.request("POST", f"/{index}/_update_by_query",
                                   json_body={"query": query, "script": script},
                                   params={"refresh": refresh, "conflicts": "proceed"},
                                   timeout=self.write_timeout)
        except ElasticsearchError as exc:
            if "index_not_found_exception" in str(exc):
                return 0
            raise
        return int((payload or {}).get("updated") or 0)

    def delete_document(self, index: str, doc_id: str, *, refresh: str = "false") -> bool:
        """True when the document existed; False when it did not."""
        try:
            self.request("DELETE", f"/{index}/_doc/{doc_id}", params={"refresh": refresh},
                         timeout=self.write_timeout)
        except ElasticsearchError as exc:
            if "404" in str(exc):
                return False
            raise
        return True

    def count(self, index: str, query: dict[str, Any]) -> int:
        try:
            payload = self.request("POST", f"/{index}/_count", json_body={"query": query})
        except ElasticsearchError as exc:
            if "index_not_found_exception" in str(exc):
                return 0
            raise
        return int((payload or {}).get("count") or 0)


def _auth_headers(api_key: str, username: str, password: str) -> dict[str, str]:
    """API key wins over basic auth; an unauthenticated cluster gets neither."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    elif username:
        raw = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {raw}"
    return headers


def _error_text(payload: Any) -> str:
    """A message that keeps the discriminator, not just the prose.

    ``type`` and ``root_cause[*].type`` are what identify a failure class; dropping them (as an
    earlier reason-only version did) makes the message unclassifiable AND unloggable.
    """
    if not isinstance(payload, dict):
        return str(payload)[:400]
    error = payload.get("error")
    if not isinstance(error, dict):
        return str(error or payload)[:400]
    parts = [str(error.get("type") or ""), str(error.get("reason") or "")]
    root = error.get("root_cause")
    if isinstance(root, list):
        for cause in root:
            if isinstance(cause, dict):
                nested = f"{cause.get('type') or ''}: {cause.get('reason') or ''}".strip(": ")
                if nested and nested not in parts:
                    parts.append(f"root_cause[{nested}]")
    return " | ".join(p for p in parts if p)[:400] or str(error)[:400]


def cloud_id_to_url(cloud_id: str) -> str:
    """Elastic Cloud ID -> the cluster's HTTPS endpoint (``""`` when it will not parse).

    A cloud id is ``<label>:<base64 of "host$es-uuid$kibana-uuid">``.
    """
    try:
        _, _, encoded = (cloud_id or "").partition(":")
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        host, es_uuid = decoded.split("$")[:2]
        return f"https://{es_uuid}.{host}"
    except Exception as exc:
        logger.debug("Unparseable Elastic Cloud ID: %s", exc)
        return ""
