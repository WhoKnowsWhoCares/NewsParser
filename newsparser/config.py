"""Application configuration loaded from environment (.env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # postgres
    db_name: str = "newsparser"
    db_user: str = "newsparser"
    db_password: str = "newsparser"
    db_host: str = "postgres"  # service name in docker-compose
    db_port: int = 5432

    # redis
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_stream: str = "news:raw"
    redis_latest_key: str = "events:latest"  # redis hash: event_id -> json summary
    redis_latest_max: int = 50  # keep last N events in the web cache
    # stream retention: keep at most this many entries (acked included)
    stream_max_len: int = 1000

    # db retention: news items older than this are pruned; orphaned events follow
    db_retention_days: int = 90

    # llm (openai-compatible endpoints, local primary + openrouter fallback)
    llm_base_url: str = "https://unsloth.home.frants.tech/v1"
    llm_api_key: str = ""
    llm_model: str = "gemma-4-E4B-it-GGUF"
    embeddings_base_url: str = ""  # empty -> same as llm_base_url
    embeddings_model: str = "unsloth/embeddinggemma-2-GGUF"
    embed_dim: int = 768

    # openrouter fallback for CHAT only (activated when openrouter_api_key is set).
    # Embeddings never fall back: vectors from different models are incomparable.
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    openrouter_chat_model: str = "google/gemini-2.5-flash-lite"

    # model keeper: enforce our models being loaded+pinned on the local server.
    # Non-empty model_keeper_evict = comma-separated models to unload when
    # found loaded (e.g. models someone loaded manually in Studio UI).
    model_keeper: bool = False
    model_keeper_interval: float = 60.0
    model_keeper_evict: str = ""

    # dedup
    dedup_threshold: float = 0.85  # cosine similarity above -> same event

    # category filter (applied after LLM tagging; comma-separated).
    # Empty = keep everything. An event whose category is NOT in the list
    # is not created: the news item is stored but has no event.
    allowed_categories: str = ""

    # queue cap: max items in redis stream (0 = unlimited).
    # Items beyond this are dropped by parsers before entering the queue.
    queue_max_length: int = 10

    # telegram parsing (user api)
    api_id: int = 0
    api_hash: str = ""
    telegram_channels: str = "interfaxonline,rbc_news,rian_ru,prime1,bcs_express"
    # StringSession string; preferred for prod (secret management).
    # Empty -> fall back to the file-based newsparser.session volume.
    telethon_session: str = ""

    # telegram bot
    bot_token: str = ""
    chat_id: str = ""  # dev subscriber

    # parsers
    poll_interval: float = 30.0

    # socks5/http proxy (telegram + openrouter fallback); empty = direct
    tg_proxy_url: str = ""

    @property
    def pg_dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}"
            f"/{self.db_name}"
        )

    @property
    def channels(self) -> list[str]:
        return [c.strip() for c in self.telegram_channels.split(",") if c.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
