# NewsParser — financial news aggregator

Сбор новостей из RSS, сайтов и Telegram-каналов → очередь (Redis Streams) →
обработка: дедупликация событий через pgvector + LLM-суммаризация →
выдача через веб-страницу, REST API и Telegram-бота.

**English** | [Русский](README.ru.md)

## Architecture (docker-compose services)

| Service | What it does | External ports |
|---|---|---|
| `newsparser-collector` | RSS (RIA), site scraping (BCS Express), Telegram channels (Telethon, user API). Pushes raw news into the `news:raw` Redis Stream. Exponential backoff on source errors | none |
| `newsparser-processor` | Consumes the stream (consumer group): link dedup → embed → nearest-event lookup in pgvector → new event (LLM summary) or update of an existing one (LLM merges details) → redis cache. Graceful degradation: when the embedder is down, items are saved with `embedding_stale` and backfilled later | none |
| `newsparser-api` | FastAPI: `/api/events` (with `?tag=` filter), `/api/events/{id}`, `/api/events/{id}/similar` (vector similar), `/api/search` (full-text RU+EN), `/api/tags`, `/api/sources`, `/healthz`, `/metrics` (Prometheus), single-page web UI with search, tag filters and a "Similar" section | **8003** (the only one) |
| `newsparser-tgbot` | Event delivery to subscribers, message editing on event updates, likes | none |
| `newsparser-postgres` | pgvector/pgvector:pg17 — events, news items, reactions, vector search (HNSW, cosine) | none |
| `newsparser-redis` | `news:raw` queue (Streams, auto-trim) + latest-events cache `events:latest` | none |

Only the API/web is exposed (`API_PORT`, default 8003); postgres, redis and all
workers communicate inside the compose docker network.

Pipeline: `source → news:raw → processor → postgres (events) + redis cache → {web, api, bot}`

### Reliability

- **Queue**: `QUEUE_MAX_LENGTH` cap enforced at the parsers (overflow → drop with a log); `STREAM_MAX_LEN` — stream retention (XTRIM).
- **DB retention**: `DB_RETENTION_DAYS` — the processor daily prunes old news items and orphaned events.
- **LLM fallback**: chat — local endpoint → OpenRouter (via proxy); embeddings — local only (vectors from different models are not comparable, fallback is intentionally absent). While the embedder is down, items accumulate in pending and are processed after recovery.
- **Multilingual dedup**: the embedding model is multilingual — a Russian and an English news item about the same event match in a shared vector space (measured: RU text ↔ RU summary 0.95, EN text ↔ RU summary 0.91 against the 0.85 threshold). Summaries are always in Russian; the source language is kept in `news_items.language`. The cross-lingual margin above the threshold is thinner — adding EN sources may require threshold calibration.
- **Healthchecks**: API — `/healthz` (real postgres/redis checks); processor and collector — heartbeat file (stale mtime → restart).
- **Metrics**: Prometheus. API — `:8003/metrics`, workers — internal :9101/:9102. Processing/dedup counters, LLM latency, queue depth.
- **Logs**: `LOG_FORMAT=json` — structured JSON logs (prod), plain text by default (dev). Daily rotation, 30-day retention.
- **Backoff**: parsers double the poll interval on source errors (up to ×16).

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

- `DEDUP_THRESHOLD` — cosine similarity threshold for merging events (default 0.85);
- `LLM_BASE_URL` / `EMBEDDINGS_BASE_URL` — OpenAI-compatible endpoints (local models);
- `OPENROUTER_API_KEY` — fallback key for the chat model (embeddings have no fallback);
- `QUEUE_MAX_LENGTH` — queue cap at the parsers (0 = unlimited, production);
- `STREAM_MAX_LEN` / `DB_RETENTION_DAYS` — stream and DB retention;
- `POLL_INTERVAL` — source poll interval, seconds;
- `TG_PROXY_URL` — socks5/http proxy (Telegram API + OpenRouter); empty → direct;
- `TELETHON_SESSION` — StringSession for prod (empty → `newsparser.session` file);
- `LOG_FORMAT=json` — structured logs;
- `METRICS_PORT` — worker /metrics port.

## Sources (current)

- RSS: [RIA Novosti](https://ria.ru/export/rss2/archive/index.xml)
- Site: [BCS Express](https://bcs-express.ru)
- Telegram: @interfaxonline, @rbc_news, @rian_ru, @prime1, @bcs_express

Adding a new source — a module in `newsparser/parsers/` + a record in
`newsparser/parsers/main.py`.

## Roadmap

- [x] Rewrite: Mongo→Postgres+pgvector, pyrogram→Telethon, sumy→LLM
- [x] Event deduplication via vector search
- [x] Web page + API + bot with message updates and likes
- [x] LLM: local endpoint (chat + embeddings) with OpenRouter chat fallback
- [x] Graceful embedding degradation + backfill
- [x] Queue/DB retention, healthchecks, Prometheus metrics, JSON logs
- [x] Similar-news search (vector) + full-text search over events
- [x] Event categorization (tags + tickers, UI filters)
- [x] Orphan-event backfill after embedder outages (auto-merge into nearest event)
- [ ] New sources (RU/EN finance)
- [ ] Targeted bot subscriptions by tag
- [ ] Dedup threshold calibration on real data

Documentation and plans: Obsidian, `AINotes/Documentation/NewsParser/`.
