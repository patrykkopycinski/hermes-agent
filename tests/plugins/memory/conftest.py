"""Shared fixtures for the es_memory provider tests."""
from __future__ import annotations

import pytest

import plugins.memory.es_memory as es_memory
import plugins.memory.es_memory.client as es_client_module
from tests.plugins.memory.fake_elasticsearch import FakeElasticsearch


@pytest.fixture
def no_yaml(monkeypatch):
    """config.yaml overlay must not leak the developer's real machine config into tests."""
    monkeypatch.setattr(es_memory, "_load_yaml_config", lambda: {})


@pytest.fixture
def fake_es(monkeypatch, no_yaml):
    client = FakeElasticsearch()
    client.install(monkeypatch, es_client_module)
    return client


@pytest.fixture
def configured(monkeypatch, tmp_path, no_yaml):
    """Point the profile home at tmp_path so config.json reads are test-local."""
    monkeypatch.setattr(es_memory, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("ES_MEMORY_URL", "https://es.example:9243")
    monkeypatch.setenv("ES_MEMORY_API_KEY", "test-key")
