# NewsParser — financial news aggregator

Сбор новостей из сайтов и Telegram-каналов → очередь (Redis Streams) →
обработка: дедупликация событий через pgvector + LLM-суммаризация →
выдача через веб-страницу, REST API и Telegram-бота.

**English** | [Русский](README.ru.md)

## Architecture (docker-compose services)

| Service | What it does | External ports |
|---|---|---|
| `newsparser-collector` | Site scraping (BCS Express), Telegram channels (Telethon, user API). Pushes raw news into the `news:raw` Redis Stream. Exponential backoff on source errors | none |
| `newsparser-processor` | Consumes the stream (consumer group): link dedup → embed → nearest-event lookup in pgvector → new event (LLM summary) or update of an existing one (LLM merges details) → redis cache. Graceful degradation: when the embedder is down, items are saved with `embedding_stale` and backfilled later | none |
| `newsparser-api` | FastAPI: `/api/events` (with `?tag=` / `?source=` filters), `/api/events/{id}`, `/api/events/{id}/similar` (vector similar), `/api/search` (full-text RU+EN), `/api/tags`, `/api/sources`, `/healthz`, `/metrics` (Prometheus), single-page web UI with search, tag/source filters, market-impact badges and a "Similar" section | **8003** (the only one) |
| `newsparser-tgbot` | Event delivery to subscribers, message editing on event updates, likes | none |
| `newsparser-postgres` | pgvector/pgvector:pg17 — events, news items, reactions, vector search (HNSW, cosine) | none |
| `newsparser-redis` | `news:raw` queue (Streams, auto-trim) + latest-events cache `events:latest` | none |

Only the API/web is exposed (`API_PORT`, default 8003); postgres, redis and all
workers communicate inside the compose docker network.

Pipeline: `source → news:raw → processor → postgres (events) + redis cache → {web, api, bot}`

### Reliability

- **Queue**: `QUEUE_MAX_LENGTH` cap enforced at the parsers (overflow → drop with a log); `STREAM_MAX_LEN` — stream retention (XTRIM).
- **DB retention**: `DB_RETENTION_DAYS` — the processor daily prunes old news items and orphaned events.
- **LLM fallback**: chat — local endpoint → OpenRouter (via proxy); embeddings — local → OpenRouter with the **same model** (`qwen/qwen3-embedding-8b` under its OR id, both paths produce comparable 4096-dim vectors). While the embedder is down, items accumulate in pending and are processed after recovery.
- **Multilingual dedup**: the embedding model is multilingual — a Russian and an English news item about the same event match in a shared vector space (measured: RU text ↔ RU summary 0.95, EN text ↔ RU summary 0.91). Summaries are always in Russian; the source language is kept in `news_items.language`. The production threshold was calibrated on real data: 0.75.
- **Healthchecks**: API — `/healthz` (real postgres/redis checks); processor and collector — heartbeat file (stale mtime → restart).
- **Metrics**: Prometheus. API — `:8003/metrics`, workers — internal :9101/:9102. Processing/dedup counters, LLM latency, queue depth.
- **Logs**: `LOG_FORMAT=json` — structured JSON logs (prod), plain text by default (dev). Daily rotation, 30-day retention.
- **Backoff**: parsers double the poll interval on source errors (up to ×16).
- **Model keeper**: `MODEL_KEEPER=1` — the processor checks every minute that the chat model and the embed model (`Qwen/Qwen3-Embedding-8B-GGUF`, 4096-dim; served locally under this id) are loaded and pinned on the local LLM server, and restores them if evicted. Loads with `alongside: true` and a reduced context (8192/2048) so the models fit in VRAM simultaneously. `MODEL_KEEPER_EVICT` — foreign models to force-unload (usually empty: the VRAM manager swaps models on demand).

## Quick start

```bash
cp .env_template .env          # fill in api_id/api_hash, bot_token
docker compose -f docker/docker-compose.yml up -d --build
# web: http://localhost:8003
```

Postgres creates the schema on first start (`docker/db/init/01_schema.sh`).
The embedding dimension is set by `EMBED_DIM` and must match the embedding
model; when changing models, recreate the `pgdata` volume.

### First run of the Telegram parser

Telethon asks for login/code interactively on first start. The login utility
prints both the file session and a ready `TELETHON_SESSION` string for prod:

```bash
uv sync
uv run python -m newsparser.tools.tg_login
```

Production auth — the `TELETHON_SESSION` variable (StringSession from env,
fits secret management); when empty, the `newsparser.session` file is used
(mounted into the container). Proxy for Telegram and OpenRouter —
`TG_PROXY_URL` (socks5/http).

## Local development

The project uses [uv](https://docs.astral.sh/uv/) (Python 3.13, see `.python-version`):

```bash
uv sync                        # create .venv and install dependencies
uv run ruff check newsparser tests
# tests (require postgres+redis running on :5434/:6380):
uv run python tests/test_processor_e2e.py       # dedup/merge/cache/reactions
uv run python tests/test_stale_backfill_e2e.py  # embedder degradation + backfill
uv run python tests/test_api_smoke.py           # API
```

CI (`.github/workflows/ci.yml`): ruff + the same tests on fresh
postgres/redis containers with the schema from the template (`EMBED_DIM=8`).

## Configuration

All parameters live in `.env` (see `.env_template`). Key ones:

- `DEDUP_THRESHOLD` — cosine similarity threshold for merging events (production: 0.75);
- `LLM_BASE_URL` / `EMBEDDINGS_BASE_URL` — OpenAI-compatible endpoints (local models);
- `OPENROUTER_API_KEY` — fallback key for chat and embeddings (embeddings fall back to the same model under its OpenRouter id);
- `OPENROUTER_EMBED_MODEL` — OpenRouter id of the embedding model (`qwen/qwen3-embedding-8b`);
- `QUEUE_MAX_LENGTH` — queue cap at the parsers (0 = unlimited, production);
- `STREAM_MAX_LEN` / `DB_RETENTION_DAYS` — stream and DB retention;
- `POLL_INTERVAL` — source poll interval, seconds;
- `TG_PROXY_URL` — socks5/http proxy (Telegram API + OpenRouter); empty → direct;
- `TELETHON_SESSION` — StringSession for prod (empty → `newsparser.session` file);
- `LOG_FORMAT=json` — structured logs;
- `METRICS_PORT` — worker /metrics port;
- `MODEL_KEEPER` / `MODEL_KEEPER_INTERVAL` / `MODEL_KEEPER_EVICT` — model pinning on the local LLM server (check interval, force-unload list).

## Sources (current)

- Site: [BCS Express](https://bcs-express.ru) (RSS disabled: the RIA feed
  brought mostly irrelevant content — `RSS_SOURCES` in `rss_parser.py` is
  empty; re-add finance feeds there to enable it again)
- Telegram: @interfaxonline, @rbc_news, @rian_ru, @prime1, @bcs_express,
  @markettwits, @if_market_news, @thewallstreetpro, @russianmacro, @truecon,
  @iticapital

Adding a new source — a module in `newsparser/parsers/` + a record in
`newsparser/parsers/main.py`.

## Roadmap

- [x] Rewrite: Mongo→Postgres+pgvector, pyrogram→Telethon, sumy→LLM
- [x] Event deduplication via vector search
- [x] Web page + API + bot with message updates and likes
- [x] LLM: local endpoint (chat + embeddings) with OpenRouter chat fallback
- [x] Graceful embedding degradation + backfill
- [x] Queue/DB retention, healthchecks, Prometheus metrics, JSON logs
- [x] Model keeper: automatic model restoration on the local LLM server
- [x] Similar-news search (vector) + full-text search over events
- [x] Event categorization (tags + tickers, UI filters)
- [x] Orphan-event backfill after embedder outages (auto-merge into nearest event)
- [x] New sources (RU finance: 6 Telegram channels added)
- [x] Dedup threshold calibration on real data (0.75)
- [x] Market-impact assessment (low/med/high badge per event card)
- [x] Source filter in the UI (same UX as tag filters)
- [ ] Targeted bot subscriptions by tag

Documentation and plans: Obsidian, `AINotes/Documentation/NewsParser/`.
