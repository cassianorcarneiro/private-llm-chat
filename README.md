# 🔒 Private LLM Chat

> A self-hosted chat portal for local LLMs. Inference never leaves your machine.

A web app that puts a ChatGPT-style interface in front of models served by [Ollama](https://ollama.com). It adds a model picker with auto-generated descriptions, opt-in web search through a self-hosted [SearXNG](https://github.com/searxng/searxng) instance, and persistent conversation history. Built for a single-GPU home server reached over a Tailscale tailnet.

<p align="center">
  <img alt="Stack" src="https://img.shields.io/badge/Stack-FastAPI%20%2B%20LangGraph%20%2B%20React-blue?style=for-the-badge">
  <img alt="Inference" src="https://img.shields.io/badge/Inference-Ollama-black?style=for-the-badge">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge">
  <img alt="Status" src="https://img.shields.io/badge/Status-Beta-orange?style=for-the-badge">
</p>

---

## 📦 How it works

1. **Pick a model.** The selector lists every chat model installed in Ollama. Each one shows a short description of what it is good at.
2. **Plan.** If the web toggle is on, the selected model decides whether a search is needed. It writes 1 to 3 search queries as structured JSON.
3. **Research.** Queries go to SearXNG. The top pages are downloaded and their main text is extracted.
4. **Rank.** Pages are split into passages. A local embedding model ranks them against the queries. Only the best passages enter the prompt.
5. **Fit the context.** Recent messages are sent verbatim. Older ones are folded into a rolling summary.
6. **Generate.** The answer streams token by token, with numbered citations when the web was used.

```
                     ┌──────────── web toggle on ────────────┐
                     │                                       ▼
  message ──▶  plan ─┤                                    research ──▶ generate ──▶ stream
                     │                                                   ▲
                     └──────────── no search needed ─────────────────────┘
```

With the web toggle off, no byte of the conversation leaves the server.

---

## ✨ Features

- **Model picker with descriptions.** Generated automatically when a model appears in Ollama, including models pulled from the CLI.
- **Model management.** Pull and delete models from the UI, with download progress.
- **GPU memory control.** See which models are resident in VRAM and unload them on demand, to free the GPU for other workloads.
- **Web search toggle.** Off by default. Answers cite their sources as `[n]`.
- **Conversation history.** Resume, rename and delete. Long chats are summarized instead of silently truncated.
- **Rich rendering.** Markdown, syntax-highlighted code and LaTeX math (KaTeX, bundled locally, no CDN).
- **Streaming.** Separate reasoning trace for thinking models, tokens/s, and a stop button that keeps the partial answer.
- **Responsive UI.** Works on desktop and phone.

---

## 🧰 What's under the hood

| Component | Technology |
|-----------|------------|
| Model serving | [`Ollama`](https://ollama.com) (host, GPU) |
| Answer pipeline | [`LangGraph`](https://langchain-ai.github.io/langgraph/) |
| API and streaming (SSE) | [`FastAPI`](https://fastapi.tiangolo.com) |
| Persistence | [`PostgreSQL`](https://www.postgresql.org) via [`SQLAlchemy`](https://www.sqlalchemy.org) (async) |
| Web search | [`SearXNG`](https://github.com/searxng/searxng) (self-hosted metasearch) |
| Page text extraction | [`trafilatura`](https://trafilatura.readthedocs.io) |
| Passage ranking | [`nomic-embed-text`](https://ollama.com/library/nomic-embed-text) via Ollama |
| Frontend | [`React`](https://react.dev) + [`Vite`](https://vite.dev), [`react-markdown`](https://github.com/remarkjs/react-markdown), [`highlight.js`](https://highlightjs.org) |
| Deployment | Docker Compose, exposed with `tailscale serve` |

```
browser ──HTTPS (tailscale serve)──▶ app :8080 ─┬─▶ Ollama (host, GPU)
                                                ├─▶ Postgres   (history, model catalog)
                                                └─▶ SearXNG ──▶ search engines
```

---

## 📋 Prerequisites

- **Docker** with Compose v2
- **Ollama** on the host, reachable from containers (see [Reaching Ollama](#-reaching-ollama-from-the-container))
- At least one chat model, e.g. `ollama pull qwen3:14b`
- Recommended: `ollama pull nomic-embed-text` for passage ranking
- **Tailscale** to access it from other devices

> **Note:** On a 16 GB GPU, models up to ~14B parameters at Q4/Q5 fit entirely in VRAM with a comfortable context window.

---

## 🚀 Quick start

### Install

```bash
git clone https://github.com/cassianorcarneiro/private-llm-chat.git /opt/private-llm-chat
cd /opt/private-llm-chat
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^SEARXNG_SECRET=.*/SEARXNG_SECRET=$(openssl rand -hex 32)/" .env
docker compose up -d --build
```

The app listens on `127.0.0.1:8088` only.

### Publish to your tailnet

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8088
# → https://<host>.<tailnet>.ts.net:8443
```

### Check

```bash
curl http://localhost:8088/api/health
# {"ollama": true, "searxng": true}
```

### 🔌 Reaching Ollama from the container

The app calls `http://host.docker.internal:11434`. Compose maps that name to the `docker0` address (`172.17.0.1`). Ollama must listen there:

```bash
sudo systemctl edit ollama
# [Service]
# Environment="OLLAMA_HOST=172.17.0.1:11434"
sudo systemctl restart ollama
```

This keeps Ollama off the LAN and the tailnet. Compose networks use other subnets (`172.18.0.0/16` and up). If a host firewall is active, allow them:

```bash
sudo ufw allow from 172.16.0.0/12 to 172.17.0.1 port 11434 proto tcp
```

---

## ⚙️ Configuration

All settings are environment variables in `.env`. See [`.env.example`](.env.example).

| Variable | Description |
|----------|-------------|
| `POSTGRES_PASSWORD` | Database password (required) |
| `SEARXNG_SECRET` | SearXNG secret key (required) |
| `APP_PORT` | Host port, bound to `127.0.0.1` (default: `8088`) |
| `OLLAMA_BASE_URL` | Ollama endpoint (default: `http://host.docker.internal:11434`) |
| `TIMEZONE` | Used for "today" in prompts (default: `America/Sao_Paulo`) |
| `DEFAULT_NUM_CTX` | Context window sent to Ollama, capped by each model's limit (default: `8192`) |
| `KEEP_ALIVE` | How long a model stays in VRAM after the last request (default: `15m`). Ollama does not yield memory to non-Ollama processes, so lower it, or unload from the UI, if other CUDA jobs share the GPU. |
| `EMBED_MODEL` | Embedding model for passage ranking (default: `nomic-embed-text`) |
| `SEARCH_AUTHORITY_BOOST` | Ranking bonus for primary sources (default: `0.15`) |
| `SEARCH_PRIMARY_DOMAINS` | JSON list of hosts treated as primary sources (default: code, package and paper hosts) |
| `CATALOG_ENABLED` | Auto-describe installed models (default: `true`) |
| `CATALOG_FETCH_REMOTE` | Fetch the model's public page to ground descriptions (default: `true`) |
| `DESCRIBER_MODEL` | Model that writes descriptions (default: the loaded one, else the model itself) |
| `DESCRIPTION_LANGUAGE` | Language of descriptions, e.g. `Brazilian Portuguese` (default: `English`) |

---

## 🔐 Privacy

| Path | What leaves the server |
|------|------------------------|
| Chat, web toggle off | Nothing |
| Chat, web toggle on | The search queries (rewritten by the model, not the raw prompt) and page requests. Search engines see your public IP. |
| New model installed | Only the model name, to ollama.com or huggingface.co. Disable with `CATALOG_FETCH_REMOTE=false`. |

There is no telemetry. To also hide your IP from search engines, route the SearXNG container through a VPN or proxy (SearXNG's `outgoing` settings).

---

## 🌐 API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/chat` | SSE stream: `meta`, `status`, `sources`, `thinking`, `token`, `stats`, `loop`, `notice`, `done`, `error` |
| `GET` | `/api/conversations` | List conversations |
| `GET` / `PATCH` / `DELETE` | `/api/conversations/{id}` | Read with messages / rename / delete |
| `GET` | `/api/models` | Installed models with metadata and descriptions |
| `POST` | `/api/models/pull` | Pull a model (SSE progress) |
| `POST` | `/api/models/{name}/describe` | Regenerate a description |
| `DELETE` | `/api/models/{name}` | Remove a model from Ollama |
| `GET` | `/api/models/loaded` | Models resident in memory (Ollama `/api/ps`) |
| `POST` | `/api/models/unload` | Unload one model (`{"name": ...}`) or all (`{}`) |
| `GET` | `/api/health` | Ollama and SearXNG reachability |

---

## 🧠 Design decisions

**Ollama, not vLLM.** vLLM pre-allocates GPU memory and serves one model per process. It optimizes throughput under many concurrent requests. A single user switching models on one GPU benefits more from Ollama's on-demand load and unload (`keep_alive`).

**The planner runs on the selected model.** That model is already resident in VRAM, so deciding whether to search costs no model swap. A separate router model would force an unload and reload on every turn on a 16 GB card. The planner uses Ollama structured outputs (JSON schema). If it fails, the app searches the raw message.

**Fetch and rerank, not snippets.** Search-engine snippets are too short to ground an answer. Pages are downloaded with a size cap and their main text is extracted. Passages are ranked by maximum cosine similarity to any query. Passages below 60% of the best score are dropped, and at most two are kept per page. Without an embedding model, ranking falls back to lexical overlap.

**Relevance is not reliability.** An SEO article titled "Latest version of X" matches the question better than the project's own release page, and may be weeks out of date. Three mechanisms counter that:

- **Query language.** The planner writes queries in the language most likely to reach primary sources (English for software and science), not necessarily the user's language.
- **Authority prior.** Primary sources get a multiplicative ranking bonus, `(1 + 0.15) × similarity`, so the bonus only reorders passages of comparable relevance. A source is primary if its host is a code, package or paper host (`github.com`, `pypi.org`, `arxiv.org`, …) or if its registrable name equals a query term (`ollama.com` for "ollama …"; `ollama-guide.net` does not qualify). Up to two primary hits beyond the top 5 are also fetched, since engines often rank them lower.
- **Dates in context.** Each passage header carries its site, publication date (from page metadata) and a primary-source flag. The model is instructed to prefer the most recent and primary source when results disagree, and to state the disagreement.

**Web content is untrusted.** Retrieved text is wrapped in delimiters with an explicit instruction to treat it as data. The app takes no actions, so prompt injection can only affect the answer text.

**`num_ctx` is always explicit.** Ollama's default context is small and truncates silently. The app sends `min(DEFAULT_NUM_CTX, model limit)` and fits the history to that budget:

```
history budget = num_ctx − response reserve − system prompt − user message − web context
```

Messages that no longer fit are merged into a rolling summary (previous summary + evicted messages). Each turn's cost stays bounded. The window always starts on a user turn, because some chat templates reject a history that opens with an assistant message.

**Runaway generation is stopped by the server.** Small models sometimes fall into a repetition loop and copy the last block they wrote until the context is full. Ollama's default repetition penalty looks only at the last 64 tokens, so a loop with a longer period is not penalized. Two guards, both independent of the model:

- **Output cap.** `num_predict` is set to the room left in the context window. Without it, Ollama keeps generating by discarding old tokens, and a looping model never stops.
- **Loop detector.** Every 48 characters, the tail of the answer is checked for a block that repeats at equal spacing at least four times (160-character probe). Digits are masked for long blocks, because loops often increment a counter (`[85]`, `[86]`, ...). On a hit, the server closes the stream, which makes Ollama stop, and keeps exactly one copy of the repeated block, so the stored answer does not poison later turns. The check needs 12 distinct characters in the probe, so separators and tables of bare numbers do not match. Three copies of a paragraph, as in "repeat this three times", do not match either.

**History lives in relational tables, not in LangGraph's checkpointer.** The checkpointer stores opaque per-step graph state. The UI must list, rename and delete conversations, which is domain data. LangGraph is used for what it adds: typed state, conditional edges and a streaming channel for node events.

**Model descriptions are grounded.** Ollama has no API for "what is this model good at". An LLM describing a model from its own weights would hallucinate, because most models are newer than its training data. So `/api/show` provides objective metadata, the model's public page provides the author's claims, and a local LLM only condenses the two.

**Model detection is level-triggered.** Ollama has no webhooks. A reconciliation loop diffs `/api/tags` against the catalog every 60 s. Models pulled from the CLI are covered too, not only those pulled through the UI.

---

## ⚠️ Limitations

- **Single user.** There is no app-level authentication. Access control is the tailnet. Do not publish the port to the LAN or the internet.
- **Local model quality.** Models that fit in 16 GB do well on everyday tasks but fall behind commercial models on long reasoning.
- **Search depends on public engines.** SearXNG aggregates several engines, but any of them can rate-limit or block it.
- **Source authority is a heuristic.** Name matching misses official sites on unrelated domains, and publication dates are only as good as the page metadata.
- **Token counts are estimated.** The budget uses ~3 characters per token, which errs on the safe side but wastes some context.
- **No schema migrations.** Tables are created with `create_all`. Add Alembic before changing the models.
- **Text only.** No file uploads or image input yet, even for vision-capable models.

---

## 📁 Project structure

```
private-llm-chat/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI routes, SSE streaming, static files
│   │   ├── graph.py           # LangGraph pipeline: plan → research → generate
│   │   ├── search.py          # SearXNG, page fetch, extraction, chunking, ranking
│   │   ├── memory.py          # Token budget, sliding window, rolling summary
│   │   ├── loopguard.py       # Repetition-loop detection for streamed output
│   │   ├── catalog.py         # Model description reconciliation loop
│   │   ├── ollama_client.py   # Async Ollama API client
│   │   ├── prompts.py         # All prompt templates
│   │   ├── db.py              # SQLAlchemy models
│   │   └── config.py          # Settings from environment
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.tsx            # State, chat streaming
│   │   ├── api.ts             # REST client and SSE parser
│   │   └── components/        # Sidebar, MessageView, Composer, ModelManager
│   └── package.json
├── searxng/
│   └── settings.yml           # Internal-only instance, JSON API enabled
├── Dockerfile                 # Multi-stage: Node build → Python runtime
├── docker-compose.yml         # app + postgres + searxng
├── .env.example
├── LICENSE
└── README.md
```

---

## 🛠️ Development

Backend (SQLite works for local runs):

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt aiosqlite
DATABASE_URL=sqlite+aiosqlite:///dev.db \
OLLAMA_BASE_URL=http://localhost:11434 \
SEARXNG_URL=http://localhost:8081 \
uvicorn app.main:app --reload --port 8080
```

Frontend (proxies `/api` to `:8080`):

```bash
cd frontend
npm install
npm run dev
```

---

## 🛣️ Roadmap

- [x] Streaming chat with persistent history
- [x] Model picker with grounded, auto-generated descriptions
- [x] Opt-in web search with citations
- [x] Rolling summary for long conversations
- [ ] Image input for vision models
- [ ] Document upload with local RAG
- [ ] Edit and regenerate messages
- [ ] Exact token counting with the model's tokenizer
- [ ] Alembic migrations

---

## 📜 License

MIT — see `LICENSE` file.

## 👤 Author

**Cassiano Ribeiro Carneiro** — [@cassianorcarneiro](https://github.com/cassianorcarneiro)

---

### 🤖 AI Assistance Disclosure

This project was designed and implemented in collaboration with [Claude](https://www.anthropic.com/claude) by Anthropic. The author defined the requirements, made the architecture and technology decisions, and reviewed the result. The project is governed by its license.

---

> *Built for anyone who wants a capable AI assistant without sending their conversations to someone else's server.*
