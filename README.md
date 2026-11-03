# local-ai-chat

A private, self-hosted chat portal for local LLMs served by [Ollama](https://ollama.com).
Inference never leaves your machine. Web search is opt-in per message and runs through a
self-hosted [SearXNG](https://github.com/searxng/searxng) instance.

Built to run on a single-GPU home server and be reached over a Tailscale tailnet.

## Features

- **Model picker with descriptions.** Every installed model gets a short "what is it good at"
  description, generated automatically when the model appears in Ollama.
- **Pull and delete models from the UI**, with download progress.
- **Web search toggle.** Off by default. When on, the model decides whether a search is needed,
  writes the queries, and answers with numbered citations.
- **Conversation history.** Resume, rename and delete. Long conversations are compressed with a
  rolling summary instead of being silently truncated.
- **Streaming** answers, separate reasoning trace for thinking models, stop button that keeps the
  partial answer, tokens/s.

## Architecture

```
browser ──HTTPS (tailscale serve)──▶ app :8080 ─┬─▶ Ollama (host, GPU)
                                    FastAPI     ├─▶ Postgres   (history, catalog)
                                    LangGraph   └─▶ SearXNG ──▶ search engines
                                    React SPA
```

| Component | Role |
|---|---|
| `backend/app/graph.py` | Answer pipeline as a LangGraph state machine: `plan → [research] → generate` |
| `backend/app/search.py` | SearXNG → fetch pages → extract main text → chunk → rank → context |
| `backend/app/memory.py` | Token budget, sliding window, incremental rolling summary |
| `backend/app/catalog.py` | Reconciliation loop that describes newly installed models |
| `frontend/` | React + Vite SPA, served as static files by the backend |

## Design decisions

**Ollama, not vLLM.** vLLM pre-allocates GPU memory and serves one model per process. It
optimizes throughput under many concurrent requests. A single user switching between models
on one GPU benefits more from Ollama's on-demand load/unload (`keep_alive`).

**The planner runs on the selected model.** That model is already resident in VRAM, so deciding
whether to search costs no model swap. A separate router model would force an unload/reload
on every turn on a 16 GB card. The planner uses Ollama structured outputs (JSON schema) and
falls back to searching the raw message if it fails.

**Fetch and rerank, not snippets.** Search-engine snippets are too short to ground an answer.
The top pages are downloaded (size-capped), their main text is extracted with `trafilatura`,
and passages are ranked against the queries with a local embedding model
(`nomic-embed-text` by default, with its task prefixes). Passages below a fraction of the
best score are dropped, and at most two passages per page are kept. Without an embedding
model, ranking falls back to lexical overlap.

**Web content is untrusted.** Retrieved text is wrapped in delimiters with an explicit
instruction to treat it as data. The app takes no actions, so the impact of prompt injection
is limited to the answer text.

**`num_ctx` is always explicit.** Ollama's default context is small and truncates silently.
The app sends `min(DEFAULT_NUM_CTX, model context length)` and fits history to that budget.
Messages that no longer fit are folded into a rolling summary (old summary + evicted
messages), so each turn's cost stays bounded.

**History lives in relational tables, not in LangGraph's checkpointer.** The checkpointer stores
opaque per-step graph state. The UI needs to list, rename and delete conversations, which is
domain data.

**Model descriptions are grounded, and detection is level-triggered.** Ollama has no API for
"what is this model good at", and no webhooks. A loop diffs `/api/tags` against the catalog,
so models pulled from the CLI are covered too. For each new model, `/api/show` provides
objective metadata and the model's public page (ollama.com or huggingface.co) provides the
author's description. A local LLM only condenses the two. Asking an LLM to describe a model
from its own weights would hallucinate, because most models are newer than its training data.

## Privacy

| Path | What leaves the server |
|---|---|
| Chat with web search off | Nothing |
| Chat with web search on | The search queries (rewritten by the model, not the raw prompt) and page requests. Engines see your public IP. |
| New model described | Only the model name, to ollama.com or huggingface.co. Disable with `CATALOG_FETCH_REMOTE=false`. |

There is no telemetry. To also hide your IP from search engines, route the SearXNG container
through a VPN or proxy (see SearXNG's `outgoing` settings).

## Requirements

- Docker with Compose v2
- Ollama on the host, reachable from containers (see below)
- At least one chat model, e.g. `ollama pull qwen3:14b`
- Optional but recommended: `ollama pull nomic-embed-text` for passage ranking

## Setup

```bash
git clone https://github.com/cassianorcarneiro/local-ai-chat.git /opt/local-ai-chat
cd /opt/local-ai-chat
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^SEARXNG_SECRET=.*/SEARXNG_SECRET=$(openssl rand -hex 32)/" .env
docker compose up -d --build
```

The app listens on `127.0.0.1:8088` only. Publish it to your tailnet with HTTPS:

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8088
# → https://<host>.<tailnet>.ts.net:8443
```

### Reaching Ollama from the container

The container calls `http://host.docker.internal:11434`, which Compose maps to the `docker0`
address (`172.17.0.1`). Ollama must listen there:

```bash
sudo systemctl edit ollama
# [Service]
# Environment="OLLAMA_HOST=172.17.0.1:11434"
sudo systemctl restart ollama
```

This keeps Ollama off the LAN and the tailnet. Compose networks use other subnets
(`172.18.0.0/16` and up), so if a host firewall is active, allow them:

```bash
sudo ufw allow from 172.16.0.0/12 to 172.17.0.1 port 11434 proto tcp
```

Check with `curl http://localhost:8088/api/health` → `{"ollama": true, "searxng": true}`.

## Configuration

All settings are environment variables; see [`.env.example`](.env.example). The most relevant:

| Variable | Default | Notes |
|---|---|---|
| `DEFAULT_NUM_CTX` | `8192` | KV cache grows linearly with it. Capped by each model's limit. |
| `KEEP_ALIVE` | `15m` | How long a model stays in VRAM after the last request. |
| `EMBED_MODEL` | `nomic-embed-text` | Passage ranking. Lexical fallback if missing. |
| `DESCRIBER_MODEL` | *(empty)* | Empty: use the loaded model, else the model being described. |
| `DESCRIPTION_LANGUAGE` | `English` | e.g. `Brazilian Portuguese`. |
| `CATALOG_FETCH_REMOTE` | `true` | Fetch model pages to ground descriptions. |

## Development

```bash
# backend (SQLite works for local runs)
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt aiosqlite
DATABASE_URL=sqlite+aiosqlite:///dev.db OLLAMA_BASE_URL=http://localhost:11434 \
SEARXNG_URL=http://localhost:8081 uvicorn app.main:app --reload --port 8080

# frontend (proxies /api to :8080)
cd frontend && npm install && npm run dev
```

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/chat` | SSE stream: `meta`, `status`, `sources`, `thinking`, `token`, `stats`, `done`, `error` |
| `GET` | `/api/conversations` | List |
| `GET/PATCH/DELETE` | `/api/conversations/{id}` | Read with messages / rename / delete |
| `GET` | `/api/models` | Installed models with metadata and descriptions |
| `POST` | `/api/models/pull` | SSE pull progress |
| `POST` | `/api/models/{name}/describe` | Regenerate a description |
| `DELETE` | `/api/models/{name}` | Remove from Ollama |
| `GET` | `/api/health` | Ollama and SearXNG reachability |

## Limitations

- Single user by design. There is no app-level authentication: access control is the tailnet.
  Do not publish the port to the LAN or the internet.
- Schema is created with `create_all`. Add Alembic before changing models.
- No file uploads or image input yet, even for vision-capable models.

## License

MIT
