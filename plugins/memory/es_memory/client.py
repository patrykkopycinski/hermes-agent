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

SEMANTIC_FIELD = "content_semantic"

# Elasticsearch accepts a `semantic_text` mapping on any licence but rejects the inference call
# itself at index time (403 "current license is non-compliant for [inference]") or when the model
# is not deployed. Existence of the inference endpoint therefore proves nothing — GET
# /_inference/<id> answers happily on a basic cluster — so the write path classifies the failure
# and drops the semantic copy instead of pre-flighting it.
_SEMANTIC_FAILURE_MARKERS = (
    "non-compliant for [inference]",
    "inference_not_found",
    "resource_not_found_exception",
    "model_deployment_not_allocated",
    "status_exception",
)


def is_semantic_unavailable(error: Exception) -> bool:
    """True when *error* means "this cluster cannot run the inference endpoint right now"."""
    text = str(error).lower()
    return any(marker in text for marker in _SEMANTIC_FAILURE_MARKERS) or "[inference]" in text


# Non-semantic mapping: everything the provider filters, sorts or displays on.
_BASE_PROPERTIES: dict[str, Any] = {
    "content": {"type": "text"},
    "namespace": {"type": "keyword"},
    "user_id": {"type": "keyword"},
    "session_id": {"type": "keyword"},
    "kind": {"type": "keyword"},
    "tags": {"type": "keyword"},
    "source": {"type": "keyword"},
    "created_at": {"type": "date"},
}


class ElasticsearchError(RuntimeError):
    """A non-2xx response, carrying the server's own error message."""


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
    ) -> None:
        self.url = re.sub(r"/+$", "", url or DEFAULT_URL)
        self._client = httpx.Client(
            base_url=self.url,
            headers=_auth_headers(api_key, username, password),
            verify=verify_certs,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None) -> Any:
        """One JSON round-trip; raises :class:`ElasticsearchError` on a non-2xx response."""
        response = self._client.request(method, path, json=json_body, params=params)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if response.is_success:
            return payload
        raise ElasticsearchError(f"{method} {path} -> {response.status_code}: {_error_text(payload)}")

    # -- Operations the provider uses ---------------------------------------

    def ping(self) -> dict[str, Any]:
        """Cluster root document (name, version, ...)."""
        return self.request("GET", "/")

    def index_exists(self, index: str) -> bool:
        return self._client.request("HEAD", f"/{index}").status_code == 200

    def create_index(self, index: str, body: dict[str, Any]) -> None:
        """Create *index*; an existing index is not an error (concurrent Hermes processes race here)."""
        try:
            self.request("PUT", f"/{index}", json_body=body)
        except ElasticsearchError as exc:
            if "resource_already_exists_exception" not in str(exc):
                raise

    def index_document(self, index: str, doc_id: str, document: dict[str, Any], *, refresh: str = "false") -> Any:
        return self.request("PUT", f"/{index}/_doc/{doc_id}", json_body=document, params={"refresh": refresh})

    def search(self, index: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Run *body* against *index* and return the raw hit list (``[]`` on a missing index)."""
        try:
            payload = self.request("POST", f"/{index}/_search", json_body=body)
        except ElasticsearchError as exc:
            if "index_not_found_exception" in str(exc):
                return []
            raise
        return list(((payload or {}).get("hits") or {}).get("hits") or [])

    def delete_document(self, index: str, doc_id: str, *, refresh: str = "false") -> bool:
        """True when the document existed; False when it did not."""
        try:
            self.request("DELETE", f"/{index}/_doc/{doc_id}", params={"refresh": refresh})
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
    """The most specific message Elasticsearch gave us, for the exception string."""
    if not isinstance(payload, dict):
        return str(payload)[:400]
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("reason") or error.get("type") or error)[:400]
    return str(error or payload)[:400]


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
