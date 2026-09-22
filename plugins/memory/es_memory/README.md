# es_memory

Elasticsearch-backed `MemoryProvider`: episodic turn capture plus long-term facts on a
real Elastic Stack, recalled through `semantic_text`/ELSER instead of a bespoke vector
store.

## Setup

```bash
hermes memory setup          # select "es_memory"
# Or manually:
hermes config set memory.provider es_memory
```

The SDK (`elasticsearch`) is an optional extra and lazy-installs on first use. To install
it up front:

```bash
pip install 'hermes-agent[es-memory]'
```

## Config

Written to `$HERMES_HOME/es_memory/config.json` by the dashboard and `hermes memory setup`;
each key also has an environment fallback, read through the profile's secret scope.

| Key | Env | Default | Notes |
|---|---|---|---|
| `url` | `ES_MEMORY_URL` | — | Endpoint URL. One of `url`/`cloud_id` is required. |
| `cloud_id` | `ES_MEMORY_CLOUD_ID` | — | Elastic Cloud deployment ID; used only when `url` is blank. |
| `api_key` | `ES_MEMORY_API_KEY` | — | Preferred credential. Stored in the profile `.env`, never in `config.json`. |
| `username` | `ES_MEMORY_USERNAME` | `elastic` | Basic auth; ignored when an API key is set. |
| `password` | `ES_MEMORY_PASSWORD` | — | Basic auth; ignored when an API key is set. |
| `retrieval` | — | `auto` | `auto` \| `semantic` \| `hybrid` \| `bm25` — see below. |
| `inference_id` | `ES_MEMORY_INFERENCE_ID` | `.elser-2-elasticsearch` | Inference endpoint behind `semantic_text`. |
| `index_prefix` | `ES_MEMORY_INDEX_PREFIX` | `hermes-memory` | First segment of the index name. |
| `top_k` | — | `8` | Documents injected per recall. |

`is_available()` requires an endpoint **and** a credential. It deliberately does not import
the SDK: the package lazy-installs during `initialize()`, and gating availability on the
import would mean it never gets installed on a sealed venv.

## Index layout

One index per profile: `<index_prefix>-<agent identity>-<sha256(hermes_home)[:10]>`. The
home hash is what guarantees two profiles both named `default` never share an index.

Two document kinds share the index:

- `kind: turn` — one immutable doc per completed turn (`sync_turn`), episodic history.
- `kind: fact` — mirrors the built-in `memory add/replace/remove` tool. `target` is
  `memory` or `user`. Superseding flips `active: false` rather than deleting, so recall can
  exclude stale facts while the history stays auditable.

A fact's document id is `sha256(index:target:content)[:24]`. Keying on content rather than
arrival order makes a retried write idempotent, and lets a `replace`/`remove` that carries
`old_text` deactivate exactly the entry it names — matching the built-in tool's per-line
semantics. A `replace` with no `old_text` falls back to superseding the whole target,
because the caller has given no way to name a single entry.

Index creation is idempotent: an existing index is inspected, never re-created.

## Retrieval strategies

| Strategy | Query | Requires |
|---|---|---|
| `semantic` | `semantic` query over `semantic_body` | An inference endpoint (ELSER) |
| `hybrid` | RRF fusion of the semantic and BM25 rankings | An inference endpoint |
| `bm25` | `match` over `content` | Nothing |
| `auto` | Semantic if the cluster accepted a `semantic_text` mapping, else BM25 | Nothing |

`auto` is the default because `semantic_text` needs an inference endpoint the cluster may
not have. Bootstrap tries the semantic mapping first; if the cluster rejects it, the index
is created lexical-only and the strategy is demoted to `bm25` for the session. An index
created by an earlier fallback keeps using BM25 — the live mapping is re-read on every
`initialize()`, so a semantic query is never sent to an index that cannot serve it.

Creating a `semantic_text` index allocates the inference endpoint, which has been measured
at ~32s on a warm cluster. That one call gets a 120s timeout; everything else uses 30s.

## Failure behaviour

A memory provider must never take the agent down. Every path here fails soft:

- `initialize()` catches everything and leaves the provider inert (`_active = False`).
- Failed writes are queued (capped at 50) and replayed at the next session boundary.
- Recall returns `""` / `[]` on any error rather than raising into the turn.
- A missing `elasticsearch` package surfaces as a failed `initialize()`, not an import error
  at plugin load.

## Tools

`es_memory_search` — semantic (or lexical) search over this profile's index, optionally
restricted to `turn` or `fact` documents.

## Status

Experimental. Before making this a profile's primary backend, verify recall quality against
whatever it is replacing on a frozen query set rather than on spot checks — retrieval
quality is the whole point of the provider, and it is the one thing a smoke test does not
measure.
