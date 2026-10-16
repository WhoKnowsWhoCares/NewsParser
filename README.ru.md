# NewsParser — агрегатор финансовых новостей

Collects news from RSS, sites and Telegram channels → queue (Redis Streams) →
processing: event deduplication via pgvector + LLM summarization →
delivery via web page, REST API and Telegram bot.

[English](README.md) | **Русский**

## Архитектура (docker-compose сервисы)

| Сервис | Что делает | Внешние порты |
|---|---|---|
| `newsparser-collector` | RSS (РИА), парсинг сайта (BCS Express), Telegram-каналы (Telethon, user API). Кладут сырые новости в Redis Stream `news:raw`. Экспоненциальный backoff при ошибках источника | нет |
| `newsparser-processor` | Берёт новости из потока (consumer group): дедуп по link → эмбеддинг → поиск ближайшего события в pgvector → новое событие (LLM-сводка) или обновление существующего (LLM вплетает детали) → кеш в Redis. Graceful degradation: если embedder недоступен, новость сохраняется с `embedding_stale` и досчитывается позже (backfill) | нет |
| `newsparser-api` | FastAPI: `/api/events` (с фильтром `?tag=`), `/api/events/{id}`, `/api/events/{id}/similar` (векторный поиск похожих), `/api/search` (полнотекстовый RU+EN), `/api/tags`, `/api/sources`, `/healthz`, `/metrics` (Prometheus), одностраничный веб-интерфейс с поиском, фильтрами и блоком «Похожие» | **8003** (единственный) |
| `newsparser-tgbot` | Рассылка событий подписчикам, редактирование сообщения при обновлении события, сбор лайков | нет |
| `newsparser-postgres` | pgvector/pgvector:pg17 — события, новости, реакции, векторный поиск (HNSW, cosine) | нет |
| `newsparser-redis` | Очередь `news:raw` (Streams, авто-trim) + кеш последних событий `events:latest` | нет |

Наружу открыт только API/веб (`API_PORT`, по умолчанию 8003); postgres, redis и
все воркеры общаются внутри docker-сети compose.

Поток: `источник → news:raw → processor → postgres (events) + redis cache → {web, api, bot}`

### Надёжность

- **Очередь**: cap `QUEUE_MAX_LENGTH` на стороне парсеров (переполнение → drop с логом); `STREAM_MAX_LEN` — ретенция стрима (XTRIM).
- **Ретенция БД**: `DB_RETENTION_DAYS` — процессор раз в сутки удаляет старые новости и события-сироты.
- **LLM fallback**: chat — локальный endpoint → OpenRouter (через прокси); embedding — только локальный (векторы разных моделей несравнимы, fallback намеренно отсутствует). При недоступности embedder'а новости копятся в pending и обрабатываются после восстановления.
- **Мультиязычный дедуп**: эмбеддинг-модель мультиязычная — русская и английская новость об одном событии сопоставляются в общем векторном пространстве (замер: RU текст ↔ RU сводка 0.95, EN текст ↔ RU сводка 0.91 при пороге 0.85). Сводки всегда на русском, язык источника сохраняется в `news_items.language`. Зазор до порога у cross-lingual пар меньше — при добавлении EN-источников возможно потребуется калибровка порога.
- **Healthchecks**: API — `/healthz` (реальные проверки postgres/redis); processor и collector — heartbeat-файл (stale mtime → restart).
- **Метрики**: Prometheus. API — `:8003/metrics`, воркеры — внутренние :9101/:9102. Счётчики обработки, дедупа, латентность LLM, глубина очереди.
- **Логи**: `LOG_FORMAT=json` — структурированные JSON-логи (prod), по умолчанию текстовые (dev). Ротация daily, retention 30 дней.
- **Backoff**: парсеры удваивают интервал опроса при ошибках источника (до ×16).
- **Model keeper**: `MODEL_KEEPER=1` — процессор каждую минуту проверяет, что gemma-4 и embeddinggemma загружены и запинены на локальном LLM-сервере, восстанавливает при выгрузке. Загрузка с `alongside: true` и урезанным контекстом (8192/2048), чтобы обе модели помещались в VRAM одновременно. `MODEL_KEEPER_EVICT` — список чужих моделей для принудительной выгрузки (обычно пусто: VRAM-менеджер сам свапает модели по запросу).

## Быстрый старт

```bash
cp .env_template .env          # заполнить api_id/api_hash, bot_token
docker compose -f docker/docker-compose.yml up -d --build
# веб: http://localhost:8003
```

При первом старте postgres создаёт схему (`docker/db/init/01_schema.sh`).
Размерность эмбеддингов задаётся `EMBED_DIM` и должна совпадать с моделью
эмбеддингов; при смене модели нужно пересоздать volume `pgdata`.

### Первый запуск Telegram-парсера

Telethon при первом запуске просит логин/код интерактивно. Утилита логина
печатает и файловую сессию, и готовую строку `TELETHON_SESSION` для прода:

```bash
uv sync
uv run python -m newsparser.tools.tg_login
```

Авторизация на проде — переменная `TELETHON_SESSION` (StringSession из env,
попадает в секрет-менеджмент); если она пуста, используется файл
`newsparser.session` (пробрасывается в контейнер volume'ом). Прокси для
Telegram и OpenRouter — `TG_PROXY_URL` (socks5/http).

## Локальная разработка

Проект использует [uv](https://docs.astral.sh/uv/) (Python 3.13, см. `.python-version`):

```bash
uv sync                        # создать .venv и установить зависимости
uv run ruff check newsparser tests
# тесты (нужны запущенные postgres+redis на :5434/:6380):
uv run python tests/test_processor_e2e.py       # дедуп/слияние/кеш/реакции
uv run python tests/test_stale_backfill_e2e.py  # деградация embedder + backfill
uv run python tests/test_api_smoke.py           # API
```

CI (`.github/workflows/ci.yml`): ruff + те же тесты на свежих
postgres/redis-контейнерах со схемой из шаблона (`EMBED_DIM=8`).

## Конфигурация

Все параметры — в `.env` (см. `.env_template`). Ключевые:

- `DEDUP_THRESHOLD` — порог косинусной близости для склейки событий (по умолчанию 0.85);
- `LLM_BASE_URL` / `EMBEDDINGS_BASE_URL` — OpenAI-совместимые endpoints (локальные модели);
- `OPENROUTER_API_KEY` — ключ fallback для chat-модели (embeddings без fallback);
- `QUEUE_MAX_LENGTH` — cap очереди на парсерах (0 = без лимита, прод);
- `STREAM_MAX_LEN` / `DB_RETENTION_DAYS` — ретенция стрима и БД;
- `POLL_INTERVAL` — период опроса источников, сек;
- `TG_PROXY_URL` — socks5/http прокси (Telegram API + OpenRouter); пусто → напрямую;
- `TELETHON_SESSION` — StringSession для прода (пусто → файл `newsparser.session`);
- `LOG_FORMAT=json` — структурированные логи;
- `METRICS_PORT` — порт /metrics воркеров;
- `MODEL_KEEPER` / `MODEL_KEEPER_INTERVAL` / `MODEL_KEEPER_EVICT` — закрепление моделей на локальном LLM-сервере (интервал проверки, список на принудительную выгрузку).

## Источники (текущие)

- RSS: [РИА Новости](https://ria.ru/export/rss2/archive/index.xml)
- Сайт: [BCS Express](https://bcs-express.ru)
- Telegram: @interfaxonline, @rbc_news, @rian_ru, @prime1, @bcs_express

Добавление нового источника — модуль в `newsparser/parsers/` + запись в
`newsparser/parsers/main.py`.

## Дорожная карта

- [x] Рефакторинг: Mongo→Postgres+pgvector, pyrogram→Telethon, sumy→LLM
- [x] Дедупликация событий через векторный поиск
- [x] Веб-страница + API + бот с обновлением сообщений и лайками
- [x] LLM: локальный endpoint (chat + embeddings) с fallback на OpenRouter для chat
- [x] Graceful degradation эмбеддингов + backfill
- [x] Ретенция очереди и БД, healthchecks, Prometheus-метрики, JSON-логи
- [x] Model keeper: автоматическое восстановление моделей на локальном LLM-сервере
- [x] Поиск по схожим новостям (векторный) + полнотекстовый поиск по событиям
- [x] Категоризация событий (теги + тикеры, фильтры в UI)
- [x] Дожим событий-сирот после простоя embedder'а (авто-мерж в ближайшее событие)
- [ ] Новые источники (RU/EN финансы)
- [ ] Адресные подписки бота по тегам
- [ ] Калибровка порога дедупликации на реальных данных

Документация и планы: Obsidian, `AINotes/Documentation/NewsParser/`.
