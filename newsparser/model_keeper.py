"""Keeps our models loaded and pinned on the local Unsloth server.

Why: the Studio UI allows loading any model manually; a big model (e.g.
Qwen3.6-35B) loaded by hand evicts ours from VRAM (only one big model fits),
and the embed/chat calls start failing or silently switching providers.

What it does (every `interval` seconds, when enabled):
  1. GET  /api/inference/loaded-models
  2. any of our models not loaded  -> POST /api/inference/load
  3. models from MODEL_KEEPER_EVICT that are loaded -> POST /api/inference/unload
  4. PUT  /api/settings/pinned-models (re-assert pinning)

The keeper never touches models it doesn't know about unless they are
explicitly listed in MODEL_KEEPER_EVICT.
"""

import asyncio

import httpx
from loguru import logger

from newsparser.config import Settings


class ModelKeeper:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        base = settings.llm_base_url.rstrip("/")
        # strip /v1 -> management API lives on the server root
        self.api = base[: -3] if base.endswith("/v1") else base
        self.headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
        self._evict = {
            self._short(m.strip())
            for m in settings.model_keeper_evict.split(",")
            if m.strip()
        }
        self._task: asyncio.Task | None = None

    # --- low-level API calls ---

    async def _loaded(self) -> set[str]:
        resp = await self.client.get(
            f"{self.api}/api/inference/loaded-models", headers=self.headers
        )
        resp.raise_for_status()
        return {m["id"] for m in resp.json().get("data", [])}

    async def _load(self, model_path: str) -> None:
        # alongside: keep already-loaded models in VRAM (no eviction swap);
        # max_seq_length: 8192 is plenty for news, cuts KV cache from ~5GB to <1GB
        payload = {
            "model_path": model_path,
            "alongside": True,
            "max_seq_length": 8192,
        }
        if self._short(model_path) == self._short(self.settings.embeddings_model):
            # embedder needs no chat context at all
            payload["max_seq_length"] = 2048
        resp = await self.client.post(
            f"{self.api}/api/inference/load",
            headers=self.headers,
            json=payload,
            timeout=600.0,
        )
        resp.raise_for_status()

    async def _unload(self, model_path: str) -> None:
        resp = await self.client.post(
            f"{self.api}/api/inference/unload",
            headers=self.headers,
            json={"model_path": model_path},
            timeout=120.0,
        )
        resp.raise_for_status()

    async def _pin(self) -> None:
        chat = self.settings.llm_model
        embed = self.settings.embeddings_model
        resp = await self.client.put(
            f"{self.api}/api/settings/pinned-models",
            headers=self.headers,
            json={
                "pinned": [chat, embed],
                "connected": [chat],
                "embedding": [embed],
            },
        )
        resp.raise_for_status()

    # --- enforcement ---

    @staticmethod
    def _short(model_id: str) -> str:
        """Canonical short name: strip the org prefix ('unsloth/...')."""
        return model_id.split("/")[-1]

    async def enforce(self) -> None:
        """One enforcement pass: load ours, evict strangers, re-pin."""
        s = self.settings
        wanted = {self._short(s.llm_model), self._short(s.embeddings_model)}
        try:
            loaded = {self._short(m) for m in await self._loaded()}
        except Exception as e:  # noqa: BLE001 - server may be down
            logger.warning("[model-keeper] loaded-models check failed: {}", e)
            return

        missing = wanted - loaded
        for model in sorted(missing):
            logger.info("[model-keeper] loading {}", model)
            try:
                await self._load(model)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    # load already in progress from a previous pass - fine
                    logger.info("[model-keeper] load {} already in progress", model)
                else:
                    logger.warning("[model-keeper] load {} failed: {}", model, e)
            except Exception as e:  # noqa: BLE001
                logger.warning("[model-keeper] load {} failed: {}", model, e)

        for model in sorted(loaded & self._evict):
            logger.info("[model-keeper] evicting {}", model)
            try:
                await self._unload(model)
            except Exception as e:  # noqa: BLE001
                logger.warning("[model-keeper] unload {} failed: {}", model, e)

        if missing or loaded & self._evict:
            try:
                await self._pin()
                logger.info("[model-keeper] pinning re-asserted")
            except Exception as e:  # noqa: BLE001
                logger.warning("[model-keeper] pin failed: {}", e)

    # --- background loop ---

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        interval = self.settings.model_keeper_interval
        while True:
            try:
                await self.enforce()
            except Exception as e:  # noqa: BLE001 - never die
                logger.warning("[model-keeper] enforce error: {}", e)
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
