"""An in-process Elasticsearch stand-in for the ``es_memory`` provider tests.

Served through ``httpx.MockTransport`` so the provider exercises its real request building,
JSON decoding and error classification — only the socket is replaced.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

# Same wording Elasticsearch uses on a basic licence; the provider classifies on it.
LICENSE_ERROR = "current license is non-compliant for [inference]"


class FakeElasticsearch:
    """Minimal index/search/delete semantics over a dict, with a togglable inference failure."""

    def __init__(self, *, inference_available: bool = True) -> None:
        self.inference_available = inference_available
        self.indices: dict[str, dict[str, Any]] = {}
        self.documents: dict[str, dict[str, dict[str, Any]]] = {}
        self.search_bodies: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []

    # -- Wiring -------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def install(self, monkeypatch, module) -> None:
        """Point *module*'s ``httpx.Client`` at this fake, preserving every other argument."""
        real_client = httpx.Client
        transport = self.transport()
        monkeypatch.setattr(
            module.httpx, "Client",
            lambda **kwargs: real_client(**{**kwargs, "transport": transport}),
        )

    # -- Request handling ---------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        body = json.loads(request.content) if request.content else {}

        if path == "/":
            return _ok({"cluster_name": "fake", "version": {"number": "9.6.0"}})
        if match := re.fullmatch(r"/([^/]+)/_doc/([^/]+)", path):
            return self._document(request.method, match.group(1), match.group(2), body)
        if match := re.fullmatch(r"/([^/]+)/_search", path):
            return self._search(match.group(1), body)
        if match := re.fullmatch(r"/([^/]+)/_count", path):
            return _ok({"count": len(self._matching(match.group(1), body.get("query") or {}))})
        if match := re.fullmatch(r"/([^/]+)/_mapping", path):
            index = match.group(1)
            if index not in self.indices:
                return _error(404, "index_not_found_exception", f"no such index [{index}]")
            return _ok({index: self.indices[index]})
        if match := re.fullmatch(r"/([^/]+)", path):
            return self._index(request.method, match.group(1), body)
        return _error(400, "illegal_argument_exception", f"unhandled path {path}")

    def _index(self, method: str, index: str, body: dict[str, Any]) -> httpx.Response:
        if method == "HEAD":
            return httpx.Response(200 if index in self.indices else 404)
        if method == "PUT":
            if index in self.indices:
                return _error(400, "resource_already_exists_exception", f"index [{index}] already exists")
            self.indices[index] = {"mappings": body.get("mappings") or {}}
            self.documents.setdefault(index, {})
            return _ok({"acknowledged": True})
        return _error(405, "illegal_argument_exception", f"{method} not supported")

    def _document(self, method: str, index: str, doc_id: str, body: dict[str, Any]) -> httpx.Response:
        if method == "DELETE":
            if self.documents.get(index, {}).pop(doc_id, None) is None:
                return _error(404, "not_found", f"no document [{doc_id}]")
            return _ok({"result": "deleted"})
        if "content_semantic" in body and not self.inference_available:
            return _error(403, "security_exception", LICENSE_ERROR)
        self.documents.setdefault(index, {})[doc_id] = body
        return _ok({"result": "created", "_id": doc_id})

    def _search(self, index: str, body: dict[str, Any]) -> httpx.Response:
        self.search_bodies.append(body)
        if index not in self.indices:
            return _error(404, "index_not_found_exception", f"no such index [{index}]")
        if _uses_semantic(body) and not self.inference_available:
            return _error(403, "security_exception", LICENSE_ERROR)
        hits = self._matching(index, body.get("query") or {})
        if body.get("sort"):
            hits.sort(key=lambda pair: pair[1].get("created_at", ""), reverse=True)
        limited = hits[: int(body.get("size") or 10)]
        return _ok({"hits": {"hits": [{"_id": doc_id, "_score": 1.0, "_source": source}
                                      for doc_id, source in limited]}})

    def _matching(self, index: str, query: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """Apply the ``bool.filter`` terms and, when present, a substring ``should`` match."""
        filters = (query.get("bool") or {}).get("filter") or []
        terms = {field: value for clause in filters for field, value in (clause.get("term") or {}).items()}
        needles = _match_needles(query)
        results = []
        for doc_id, source in self.documents.get(index, {}).items():
            if any(source.get(field) != value for field, value in terms.items()):
                continue
            if needles and not any(n.lower() in str(source.get("content", "")).lower() for n in needles):
                continue
            results.append((doc_id, source))
        return results


def _match_needles(query: dict[str, Any]) -> list[str]:
    """Words a ``should`` clause asks for; empty list means "match everything that filtered"."""
    out = []
    for clause in (query.get("bool") or {}).get("should") or []:
        if "match" in clause:
            out.extend(str((clause["match"].get("content") or {}).get("query", "")).split())
        elif "semantic" in clause:
            out.extend(str(clause["semantic"].get("query", "")).split())
    return out


def _uses_semantic(body: dict[str, Any]) -> bool:
    return any("semantic" in clause for clause in ((body.get("query") or {}).get("bool") or {}).get("should") or [])


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _error(status: int, error_type: str, reason: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"type": error_type, "reason": reason}})
