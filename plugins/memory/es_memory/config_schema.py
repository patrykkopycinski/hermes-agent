"""Elasticsearch memory's declared config surface — rendered by the generic desktop panel."""

from plugins.memory.config_schema import (
    KIND_BOOL, KIND_NUMBER, KIND_SECRET, KIND_SELECT, KIND_TEXT,
    ProviderConfigSchema, ProviderField, ProviderFieldOption,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="es_memory",
    label="Elasticsearch",
    docs_url="https://www.elastic.co/docs/api/doc/elasticsearch",
    fields=(
        ProviderField(
            key="url", label="Cluster URL", kind=KIND_TEXT, default="http://localhost:9200",
            description="Elasticsearch HTTP endpoint. Ignored when a Cloud ID is set.",
            placeholder="http://localhost:9200", env_fallbacks=("ES_MEMORY_URL",), inline=True,
        ),
        ProviderField(
            key="api_key", label="API key", kind=KIND_SECRET, env_key="ES_MEMORY_API_KEY",
            description="Base64 API key (preferred). Falls back to username/password when unset.",
            placeholder="Enter Elasticsearch API key", inline=True,
        ),
        ProviderField(
            key="index", label="Index", kind=KIND_TEXT, default="hermes-memory",
            description="Index memories are written to; created on first use if absent.",
            env_fallbacks=("ES_MEMORY_INDEX",), inline=True,
        ),
        ProviderField(
            key="retrieval", label="Retrieval", kind=KIND_SELECT, default="auto",
            description="How recall queries the cluster.",
            options=(
                ProviderFieldOption("auto", "Auto", "Semantic while the cluster will run it, else BM25"),
                ProviderFieldOption("semantic", "Semantic", "semantic_text / ELSER only — fails closed if unavailable"),
                ProviderFieldOption("bm25", "BM25", "Lexical only; no inference endpoint required"),
            ),
            inline=True,
        ),
        ProviderField(
            key="cloud_id", label="Cloud ID", kind=KIND_TEXT, group="Connection",
            description="Elastic Cloud deployment ID; takes precedence over Cluster URL.",
            env_fallbacks=("ES_MEMORY_CLOUD_ID",),
        ),
        ProviderField(
            key="username", label="Username", kind=KIND_TEXT, group="Connection",
            description="Basic-auth user, used only when no API key is set.",
            env_fallbacks=("ES_MEMORY_USERNAME",),
        ),
        ProviderField(
            key="password", label="Password", kind=KIND_SECRET, env_key="ES_MEMORY_PASSWORD",
            group="Connection", description="Basic-auth password.",
        ),
        ProviderField(
            key="verify_certs", label="Verify TLS certificates", kind=KIND_BOOL, default="true",
            group="Connection", description="Turn off only for a self-signed development cluster.",
        ),
        ProviderField(
            key="timeout", label="Request timeout (s)", kind=KIND_NUMBER, default="15",
            group="Connection", description="Per-request timeout for reads and writes.",
        ),
        ProviderField(
            key="write_timeout", label="Write timeout (s)", kind=KIND_NUMBER, default="60",
            group="Connection",
            description="Timeout for indexing, updates and deletes.",
            info="Far longer than the read timeout because a semantic_text write runs ELSER "
                 "inference inline (~8.5s for one document, and concurrent writes queue). Too "
                 "short a budget reports failure for a write Elasticsearch actually stored.",
        ),
        ProviderField(
            key="bootstrap_timeout", label="Index bootstrap timeout (s)", kind=KIND_NUMBER, default="90",
            group="Connection",
            description="Timeout for the one-off index creation.",
            info="Creating an index with a semantic_text field makes Elasticsearch allocate the "
                 "inference endpoint inline, measured at ~32s on a real deployment. This is "
                 "deliberately far longer than the per-request timeout.",
        ),
        ProviderField(
            key="ssh_tunnel_host", label="SSH tunnel host", kind=KIND_TEXT, group="Connection",
            env_fallbacks=("ES_MEMORY_SSH_TUNNEL_HOST",),
            description="Documentation only — Hermes never opens the tunnel itself.",
            info="When the Cluster URL points at a loopback port served by an SSH forward, set the "
                 "host here and Hermes prints the exact `ssh -L` command in connection errors "
                 "instead of a bare connection-refused.",
        ),
        ProviderField(
            key="ssh_remote_port", label="SSH remote ES port", kind=KIND_NUMBER, default="9220",
            group="Connection",
            description="Remote-side Elasticsearch port used when rendering the tunnel command.",
        ),
        ProviderField(
            key="inference_id", label="Inference endpoint", kind=KIND_TEXT,
            default=".elser-2-elasticsearch", group="Retrieval",
            description="Inference endpoint backing the semantic_text field.",
            info="Elasticsearch 8.16+ ships '.elser-2-elasticsearch' preconfigured, but running it "
                 "needs a licence tier that permits inference; point this at your own endpoint to "
                 "use a different sparse or dense model.",
        ),
        ProviderField(
            key="top_k", label="Recall results", kind=KIND_NUMBER, default="5", group="Retrieval",
            description="Memories injected per turn.",
        ),
        ProviderField(
            key="query_rewrite", label="Rewrite recall queries", kind=KIND_BOOL, default="false",
            group="Retrieval",
            description="Rewrite each turn's message into a retrieval question (one auxiliary LLM call).",
        ),
        ProviderField(
            key="namespace", label="Namespace", kind=KIND_TEXT, group="Retrieval",
            description="Isolation key for this profile's memories. Defaults to the profile name.",
            env_fallbacks=("ES_MEMORY_NAMESPACE",),
        ),
    ),
)
