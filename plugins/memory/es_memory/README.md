# Elasticsearch memory (`es_memory`)

> ## Cutover precondition — DO NOT SKIP
>
> **Status: experimental dogfood. Do NOT set `memory.provider: es_memory` for real use yet.**
> `memory.provider` stays `hindsight` until this gate passes.
>
> Before ever flipping it:
>
> 1. Build the Memory QA eval: a frozen set of 150+ (query, expected-answer) pairs drawn from
>    real session history.
> 2. Run it against both Hindsight (current) and `es_memory` with Recall@10 (or equivalent).
> 3. `es_memory` must **match or beat** Hindsight's R@10 on that frozen set — not on vibes, not
>    on a handful of manual spot-checks.
> 4. Only then flip `memory.provider`, and only with the user's explicit go-ahead.
>
> Until that eval exists and passes, this provider is proof that the ES path *can* work — not
> evidence that it *should* replace Hindsight today. (Inherited verbatim in intent from the
> retired `es-memory` plugin, whose gate this supersedes.)


Long-term memory backed by an Elasticsearch index. Turns and explicit facts are stored as
documents; recall is lexical (BM25), semantic (`semantic_text` / ELSER), or `auto` — which
prefers semantic and falls back to BM25 when the cluster will not run the inference endpoint.

No extra install step: the provider talks to the ES REST API over `httpx`, which Hermes already
depends on. The official `elasticsearch` Python client is **not** required.

## Install

This ships as a **user plugin**, not in-tree: `plugins/AGENTS.md` closed `plugins/memory/` to new
backends in May 2026. Drop the directory into the profile's plugin root and Hermes discovers it
by directory name:

```bash
cp -R plugins/memory/es_memory ~/.hermes/plugins/es_memory
hermes memory setup      # select "es_memory"
```

Discovery is `plugins/memory/__init__.py`: `$HERMES_HOME/plugins/<name>/` is scanned after the
bundled providers, the directory name *is* the provider name and the `memory.provider` value, and
the module is imported under a synthetic `_hermes_user_memory.<name>__source_<hash>` package so
the relative imports between `__init__.py`, `client.py` and `tool_schemas.py` resolve normally.
`config_schema.py` is separately loaded **by path** for the dashboard panel, which is why it may
import only `plugins.memory.config_schema` and nothing from the agent runtime.

Because bundled providers win name collisions, an in-tree `plugins/memory/es_memory/` would
shadow this copy.

Then in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: es_memory
```

## Configuration

Settings resolve in this order: `$HERMES_HOME/es_memory/config.json` (written by the dashboard
panel and `save_config`) → `memory.es_memory` in `config.yaml` → the env fallback → the default.
Secrets live in the env store only.

| Key | Default | Notes |
|---|---|---|
| `url` | `http://localhost:9200` | Cluster endpoint. Env fallback `ES_MEMORY_URL`. |
| `api_key` | — | Base64 API key. Secret, stored as `ES_MEMORY_API_KEY`. |
| `cloud_id` | — | Elastic Cloud deployment ID; takes precedence over `url`. |
| `username` / `password` | — | Basic auth, used only when no API key is set (`ES_MEMORY_PASSWORD`). |
| `verify_certs` | `true` | Turn off only for a self-signed development cluster. |
| `index` | `hermes-memory` | Created with an explicit mapping on first use if absent. |
| `retrieval` | `auto` | `auto` \| `semantic` \| `bm25`. |
| `inference_id` | `.elser-2-elasticsearch` | Inference endpoint behind the `semantic_text` field. |
| `top_k` | `5` | Memories injected per turn. |
| `query_rewrite` | `false` | Rewrite each turn into a retrieval question (one auxiliary LLM call). |
| `namespace` | profile name | Isolation key; see below. |
| `timeout` | `15` | Per-request (read) timeout, seconds. |
| `write_timeout` | `60` | Indexing/update/delete timeout; see below. |
| `bootstrap_timeout` | `90` | Index-creation timeout; see below. |
| `ssh_tunnel_host` | — | Documentation only; see "Tunnelled clusters". |
| `ssh_remote_port` | `9220` | Remote ES port used when rendering the tunnel command. |

`bootstrap_timeout` is deliberately far larger than `timeout`: creating an index whose mapping
declares a `semantic_text` field makes Elasticsearch allocate the inference endpoint inline,
measured at ~32s on a real deployment. Sharing the per-request timeout would time the create
out on exactly the licensed clusters semantic recall needs.

`write_timeout` is separate from `timeout` for the same reason at a different scale: a
`semantic_text` write runs ELSER inference inline and is CPU-bound. One sequential write
measured ~8.5s, and at 5 concurrent writes 1,028 of 1,142 exceeded a 15s budget — while
Elasticsearch had in fact stored the document, so the caller saw a failure for a write that
succeeded. Concurrency queues the inference (5 × 8.5s ≈ 43s), so 60s covers the measured worst
case with roughly 40% headroom plus the up-to-1s `refresh=wait_for` wait. Reads keep the short
budget on purpose: a slow search should fail fast and degrade rather than stall a turn.

Because a timed-out write may still have landed, **every write path is content-addressed** —
`sha256(namespace:scope:content)`, where the scope is the session id for turns and the `kind`
for `es_memory_remember`, and `namespace:target:content` for mirrored memory-tool facts. A
retry of a write that actually succeeded is therefore an idempotent overwrite, not a second
copy. The same addressing is what lets a `replace`/`remove` carrying `old_text` find the exact
entry it supersedes.

## Tunnelled clusters (e.g. VP ES over SSH)

When the cluster is only reachable through an SSH local-forward, run the forward yourself and
point `url` at the local end:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 19221:127.0.0.1:9220 m1max
```

```yaml
memory:
  es_memory:
    url: http://127.0.0.1:19221
    ssh_tunnel_host: m1max      # documentation only
    ssh_remote_port: 9220
```

Pick a local port nothing else uses — `19221` rather than `19220`, which the AlertZero ingest
scripts already take; two clients sharing a forward silently ride each other's tunnel.

`ssh_tunnel_host` changes no behaviour. Its only effect is that connection failures and
`unavailable_reason()` print the exact `ssh -L` command instead of a bare connection-refused.

**Hermes does not open the tunnel.** The retired `es-memory` plugin spawned `ssh -N -L …` from
`initialize()` with `subprocess.Popen`; a memory provider cannot win that lifecycle — the child
outlives a crashed agent, a second profile silently rides the first one's forward, and startup
blocks on a fixed sleep waiting for the forward to come up. Owning the ssh process is the
user's job (or `autossh`, or a `ControlMaster` in `~/.ssh/config`).

### Retrieval modes

- **`bm25`** — lexical only. Works on any cluster, no inference endpoint needed.
- **`semantic`** — `semantic_text` only. Fails closed when inference is unavailable, so you
  notice a broken deployment instead of silently losing recall quality.
- **`auto`** (default) — uses semantic while the cluster will run it, and downgrades to BM25 the
  first time it will not.

A downgrade is visible in `es_memory_status`: `retrieval` is what is active now,
`retrieval_configured` is what you asked for, and `semantic_degraded` is true when they differ.

The downgrade lives on the **write and query paths**, not in a pre-flight probe, because a
pre-flight probe cannot answer the question. Elasticsearch accepts a `semantic_text` mapping on
any licence, and `GET /_inference/.elser-2-elasticsearch` happily returns the preconfigured
endpoint — but a basic-licence cluster then rejects the actual inference at index time with
`403 current license is non-compliant for [inference]`. So `content` is never `copy_to`-ed into
the semantic field: the provider writes the semantic copy as an explicit field, and if a write
is refused it drops that copy for the rest of the process and retries the same document as a
plain one. The memory is stored either way; only recall quality degrades.

Failures are classified from the **whole** error — `type`, `root_cause[*].type`/`reason`,
`caused_by` and the status code — never from `reason` alone. An endpoint that is registered but
whose deployment is not running answers `404` with `type: resource_not_found_exception` and a
`reason` that names no failure class, so a reason-only classifier treated it as a hard error:
every write was dropped while status still reported `semantic+bm25`. A `400` is deliberately
excluded from the degrade — a malformed semantic query is our own bug, and falling back would
hide it behind quietly worse recall.

The mapping is also reconciled against the live index on every `initialize()`: an index created
before semantic was configured has no `semantic_text` field, so recall queries BM25 rather than a
field that is not mapped.

## Isolation

Every document carries a `namespace` keyword, and every read, count and delete is filtered by it.
It defaults to the profile directory name (`default` for the default profile), so a multiplexed
secondary never recalls the default profile's memories even when both point at one cluster and
index. Set `namespace` explicitly to share one slice across profiles on purpose.

Documents also carry `user_id` and `session_id` for provenance; those are stored but not used as
isolation keys.

## Fact supersession

The built-in memory tool edits *lines in a block*, so mirroring its writes as plain appends
would leave recall answering with text the user just rewrote. Instead, mirrored entries carry
`target` (`memory` or `user`) and `active`, and a write retires what it replaced:

| Call | Effect |
|---|---|
| `add` | Writes a new active entry. |
| `replace` **with** `old_text` | Retires exactly the named entry, then writes the new one. |
| `replace` **without** `old_text` | Retires every active entry for that `target`, then writes the new one. |
| `remove` **with** `old_text` | Retires exactly the named entry. |
| `remove` **without** `old_text` | Retires every active entry for that `target`. |

`memory_manager.notify_memory_tool_write` forwards `old_text` in the metadata when the tool
call carried one; the whole-target fallback is deliberately conservative, because a caller that
names no predecessor gives no way to identify the single line it rewrote, and leaving a stale
line active beside the new one is the worse failure.

Retiring flips `active` to `false` rather than deleting: the history stays auditable, and every
read (`es_memory_search`, `es_memory_list`, per-turn recall, the `es_memory_status` count)
excludes `active: false`. The exclusion is a `must_not` on `false`, not a requirement that it be
`true`, so documents written before this field existed still match.

Mirrored entries use a deterministic id — `sha256(namespace:target:content)` — so a retried or
replayed `notify_memory_tool_write` overwrites in place instead of leaving two active copies of
the same line, and `old_text` addresses the superseded entry directly (with a content-match
fallback for entries written by another writer). Agent-authored `es_memory_remember` facts carry
`target: ""` and are therefore never swept up by a built-in memory-tool rewrite.

## Index mapping

```json
{
  "content":          { "type": "text" },
  "content_semantic": { "type": "semantic_text", "inference_id": ".elser-2-elasticsearch" },
  "namespace":        { "type": "keyword" },
  "user_id":          { "type": "keyword" },
  "session_id":       { "type": "keyword" },
  "kind":             { "type": "keyword" },
  "target":           { "type": "keyword" },
  "active":           { "type": "boolean" },
  "tags":             { "type": "keyword" },
  "source":           { "type": "keyword" },
  "created_at":       { "type": "date" }
}
```

`content_semantic` is omitted in BM25 mode. `kind` is one of `turn`, `fact`, `preference`,
`decision`; `source` records which path wrote the document (`turn`, `tool`, `memory_tool`);
`target` and `active` drive supersession (above).

## Tools

| Tool | Purpose |
|---|---|
| `es_memory_remember` | Store a durable fact, preference, or decision. |
| `es_memory_search` | Search memories (semantic + BM25, or BM25 alone). |
| `es_memory_list` | List recent memories, newest first. |
| `es_memory_forget` | Delete one memory by id. |
| `es_memory_status` | Cluster reachability, index, retrieval mode, document count. |

## Notes

- Completed turns are written by `sync_turn`, and built-in memory-tool writes are mirrored by
  `on_memory_write`. Both are best-effort: an unreachable cluster degrades recall, it never
  fails a turn.
- Writes are skipped outside the primary agent context (subagent, cron, flush).
- Recall runs in a background thread started through `spawn_context_thread`, so it inherits the
  calling profile's scope.
