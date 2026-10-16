"""LLM client: OpenAI-compatible chat completions + embeddings with fallback.

Primary provider: local endpoint (unsloth.home.frants.tech).
Fallback: OpenRouter when local is unreachable or returns an error.
Both endpoints must speak the OpenAI API format.
"""

import json
import time

import httpx
from loguru import logger

from newsparser.config import Settings
from newsparser.metrics import EMBED_FAILURES, LLM_LATENCY

CATEGORIES = (
    "ЦБ (центробанк/ключевая ставка), Минфин, инфляция, банки, акции, облигации, "
    "валюта, нефть/газ, металлы, крипто, компании, M&A, IPO, санкции, "
    "геополитика, налоги, бюджет, недвижимость, ритейл, техно, экономика РФ, "
    "экономика мира, статистика"
)

# categories outside the financial scope; used with ALLOWED_CATEGORIES filter
NON_FINANCIAL = (
    "СПОРТ, ПОГОДА, КУЛЬТУРА, ПРОИСШЕСТВИЯ, ШОУБИЗ, НАУКА, ЗДРАВООХРАНЕНИЕ, "
    "ТУРИЗМ, ТРАНСПОРТ, ОБЩЕСТВО"
)

TAG_PROMPT_APPENDIX = (
    "\n\nПосле сводки выдели теги и верни СТРОГО валидный JSON без markdown-обёрток:\n"
    '{{"summary": "<сводка>", "tags": ["<тег>", ...]}}\n'
    "Правила тегов:\n"
    "- первый тег — одна категория. Для новостей о финансах и экономике — из "
    "списка: {categories}. Если новость НЕ про финансы/экономику (спорт, погода, "
    "культура, происшествия, шоу-бизнес и т.п.) — выбери подходящую категорию из "
    "списка: " + NON_FINANCIAL + ";\n"
    "- если упоминается публичная компания — добавь её биржевой тикер прописными "
    "(Сбербанк → SBER, Газпром → GAZP, Яндекс → YDEX, Лукойл → LKOH, "
    " Норникель → GMKN, Северсталь → CHMF, Татнефть → TATN, Новатэк → NVTK, "
    " Роснефть → ROSN, ВТБ → VTBR, Магнит → MGNT, X5 → X5, "
    " Apple → AAPL, Microsoft → MSFT, Google/Alphabet → GOOGL, Amazon → AMZN, "
    " Meta → META, Tesla → TSLA, NVIDIA → NVDA); не более 5 тикеров;\n"
    "- только то, что реально упомянуто; без пояснений внутри массива."
)

SUMMARY_NEW_EVENT_PROMPT = (
    "Ты редактор финансовых новостей. Ниже новость (RU или EN). "
    "Напиши краткую сводку события на русском языке (2-3 предложения): "
    "что произошло, ключевые цифры и факты. Без преамбул, только сводка." + TAG_PROMPT_APPENDIX +
    "\n\nНовость:\nЗаголовок: {title}\nТекст: {text}"
)

SUMMARY_MERGE_PROMPT = (
    "Ты редактор финансовых новостей. Ниже существующая сводка события и новая новость "
    "по тому же событию. Обнови сводку, вплетая новые детали и факты из новой новости. "
    "Ответ на русском языке, 2-4 предложения, только обновлённая сводка." + TAG_PROMPT_APPENDIX +
    "\nДополнительно: сохрани релевантные теги существующей сводки и добавь новые.\n\n"
    "Существующая сводка:\n{summary}\nСуществующие теги: {tags}\n\n"
    "Новая новость:\nЗаголовок: {title}\nТекст: {text}"
)


class _Endpoint:
    """One OpenAI-compatible chat provider (base url + key + model)."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        chat_model: str,
        proxy: str | None = None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.chat_model = chat_model
        self.proxy = proxy


class _EmbedEndpoint:
    """Embedding provider — always the local endpoint, never falls back."""

    def __init__(self, name: str, base_url: str, api_key: str, embed_model: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.embed_model = embed_model
        self.proxy = None  # local endpoint: never proxied


class LLMClient:
    """Chat with local -> openrouter fallback; embeddings local-only."""

    def __init__(self, settings: Settings):
        self.settings = settings
        timeout = httpx.Timeout(120.0, connect=10.0)
        self._client = httpx.AsyncClient(timeout=timeout)

        self._embed = _EmbedEndpoint(
            "local",
            settings.embeddings_base_url or settings.llm_base_url,
            settings.llm_api_key,
            settings.embeddings_model,
        )
        self._endpoints: list[_Endpoint] = [
            _Endpoint(
                "local",
                settings.llm_base_url,
                settings.llm_api_key,
                settings.llm_model,
            )
        ]
        if settings.openrouter_api_key:
            # OpenRouter blocks some regions by IP; route via TG proxy when set
            self._endpoints.append(
                _Endpoint(
                    "openrouter",
                    settings.openrouter_base_url,
                    settings.openrouter_api_key,
                    settings.openrouter_chat_model,
                    proxy=settings.tg_proxy_url or None,
                )
            )
        else:
            logger.warning("OpenRouter key not set: no chat fallback available")

    async def close(self) -> None:
        await self._client.aclose()

    def raw_client(self) -> httpx.AsyncClient:
        """Shared httpx client (used by the model keeper's admin calls)."""
        return self._client

    async def _post(self, ep: _Endpoint | _EmbedEndpoint, path: str, payload: dict) -> dict:
        """POST to ep.base_url/path. For proxied endpoints a one-off client is used."""
        url = f"{ep.base_url}/{path}"
        headers = {"Authorization": f"Bearer {ep.api_key}"}
        if ep.proxy:
            proxy = ep.proxy.replace("socks5://", "socks5://")
            async with httpx.AsyncClient(timeout=self._client.timeout, proxy=proxy) as client:
                resp = await client.post(url, json=payload, headers=headers)
        else:
            resp = await self._client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def chat(self, prompt: str, max_tokens: int = 2048) -> str:
        """Chat completion, trying each endpoint in order until one succeeds.

        max_tokens must leave room for reasoning models (gemma-4 etc.) that
        spend the budget on internal thinking before answering.
        """
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        last_err: Exception | None = None
        for ep in self._endpoints:
            start = time.monotonic()
            try:
                data = await self._post(
                    ep,
                    "chat/completions",
                    payload | {"model": ep.chat_model},
                )
                LLM_LATENCY.labels("chat", ep.name).observe(time.monotonic() - start)
                msg = data["choices"][0]["message"]
                # reasoning models may put everything into reasoning_content
                # and leave content empty when the budget runs out mid-think
                text = (msg.get("content") or "").strip()
                if not text:
                    text = (msg.get("reasoning_content") or "").strip()
                if not text:
                    raise ValueError("empty content and reasoning_content")
                return text
            except Exception as e:  # noqa: BLE001 - any provider error -> fallback
                last_err = e
                logger.warning("Chat via {} failed: {}, trying fallback", ep.name, e)
        raise RuntimeError(f"All LLM endpoints failed for chat: {last_err}")

    async def embed(self, text: str) -> list[float]:
        """Embedding: local endpoint ONLY, never fall back to another provider.

        Vectors from different embedding models are not comparable even at the
        same dimension: mixing providers would corrupt cosine-similarity dedup
        against previously stored vectors. If local is down, raise — the item
        stays pending in redis and is retried when local recovers.
        """
        ep = self._embed
        payload = {"input": text[:8000].strip() or "n/a"}
        start = time.monotonic()
        try:
            data = await self._post(
                ep,
                "embeddings",
                payload | {"model": ep.embed_model},
            )
        except Exception:
            EMBED_FAILURES.inc()
            raise
        LLM_LATENCY.labels("embed", ep.name).observe(time.monotonic() - start)
        embedding = data["data"][0]["embedding"]
        if len(embedding) != self.settings.embed_dim:
            logger.warning(
                "Embedding dim mismatch ({}): got {}, expected {} (check EMBED_DIM)",
                ep.name, len(embedding), self.settings.embed_dim,
            )
        return embedding

    @staticmethod
    def _parse_summary_json(text: str) -> tuple[str, list[str]]:
        """Extract (summary, tags) from a model reply; fall back to raw text.

        Models sometimes wrap JSON in ```json fences or prepend chatter —
        find the outermost {...} and parse that.
        """
        text = text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                summary = str(data.get("summary", "")).strip()
                tags_raw = data.get("tags", [])
                if summary and isinstance(tags_raw, list):
                    tags = [str(t).strip().upper() for t in tags_raw if str(t).strip()]
                    # cap: 1 category + tickers
                    return summary, list(dict.fromkeys(tags))[:6]
            except json.JSONDecodeError:
                pass
        # model ignored the JSON contract: treat the whole reply as summary
        return text, []

    async def summarize_new_event(self, title: str, text: str) -> tuple[str, list[str]]:
        """Summarize + tag a new event. Returns (summary, tags)."""
        if not text:
            return title, []
        raw = await self.chat(
            SUMMARY_NEW_EVENT_PROMPT.format(
                title=title, text=text, categories=CATEGORIES
            )
        )
        return self._parse_summary_json(raw)

    async def summarize_merge(
        self, summary: str, title: str, text: str, tags: list[str] | None = None
    ) -> tuple[str, list[str]]:
        """Merge a new item into an existing event. Returns (summary, tags)."""
        raw = await self.chat(
            SUMMARY_MERGE_PROMPT.format(
                summary=summary,
                tags=", ".join(tags or []) or "нет",
                title=title,
                text=text,
                categories=CATEGORIES,
            )
        )
        new_summary, new_tags = self._parse_summary_json(raw)
        if not new_tags and tags:
            # model dropped tags: keep the old ones
            new_tags = tags
        return new_summary, new_tags
