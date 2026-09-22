"""ES memory's declared config surface — rendered by the generic desktop panel.

Pure data: this module is loaded BY PATH by the web server and may import only
``plugins.memory.config_schema`` (never the provider ``__init__``, which pulls in
the agent runtime). Keep the defaults here in sync with ``__init__._CONFIG_SPEC``.
"""

from plugins.memory.config_schema import (
    KIND_NUMBER, KIND_SECRET, KIND_SELECT, KIND_TEXT, ProviderConfigSchema, ProviderField,
    ProviderFieldOption,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="es_memory",
    label="Elasticsearch",
    docs_url="https://www.elastic.co/guide/en/elasticsearch/reference/current/semantic-text.html",
    fields=(
        ProviderField(
            key="url", label="Endpoint URL", kind=KIND_TEXT,
            description="Elasticsearch endpoint URL. Leave blank when using a Cloud ID.",
            placeholder="https://localhost:9200", env_fallbacks=("ES_MEMORY_URL",), inline=True,
        ),
        ProviderField(
            key="cloud_id", label="Cloud ID", kind=KIND_TEXT,
            description="Elastic Cloud deployment ID. Used only when no endpoint URL is set.",
            env_fallbacks=("ES_MEMORY_CLOUD_ID",), group="Connection",
        ),
        ProviderField(
            key="api_key", label="API key", kind=KIND_SECRET, env_key="ES_MEMORY_API_KEY",
            description="Base64 API key from Kibana → Stack Management → API keys. Preferred over basic auth.",
            placeholder="Enter Elasticsearch API key", inline=True,
        ),
        ProviderField(
            key="username", label="Username", kind=KIND_TEXT, default="elastic",
            description="Basic-auth user. Ignored when an API key is set.",
            env_fallbacks=("ES_MEMORY_USERNAME",), group="Connection",
        ),
        ProviderField(
            key="password", label="Password", kind=KIND_SECRET, env_key="ES_MEMORY_PASSWORD",
            description="Basic-auth password. Ignored when an API key is set.", group="Connection",
        ),
        ProviderField(
            key="retrieval", label="Retrieval strategy", kind=KIND_SELECT, default="auto",
            description="How recall queries the index.",
            options=(
                ProviderFieldOption("auto", "Auto",
                                    "Semantic when the cluster accepts a semantic_text mapping, else BM25"),
                ProviderFieldOption("semantic", "Semantic", "ELSER sparse-vector retrieval over semantic_text"),
                ProviderFieldOption("hybrid", "Hybrid (RRF)", "Reciprocal rank fusion of semantic and BM25"),
                ProviderFieldOption("bm25", "BM25", "Lexical only — no inference endpoint required"),
            ),
            inline=True,
        ),
        ProviderField(
            key="inference_id", label="Inference endpoint", kind=KIND_TEXT, default=".elser-2-elasticsearch",
            description="Inference endpoint backing the semantic_text field. Ignored for BM25.",
            env_fallbacks=("ES_MEMORY_INFERENCE_ID",), group="Retrieval",
        ),
        ProviderField(
            key="index_prefix", label="Index prefix", kind=KIND_TEXT, default="hermes-memory",
            description="Indices are named <prefix>-<agent identity>-<profile hash> for per-profile isolation.",
            env_fallbacks=("ES_MEMORY_INDEX_PREFIX",), group="Retrieval",
        ),
        ProviderField(
            key="top_k", label="Recall results", kind=KIND_NUMBER, default="8",
            description="How many documents prefetch injects into the turn.", group="Retrieval",
        ),
    ),
)
